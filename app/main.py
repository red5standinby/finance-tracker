"""FastAPI app: import, dashboard, transactions, categories, rules."""
from __future__ import annotations

import io
import json
import os
import re
import difflib
import uuid
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, timedelta

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import categorize as cat
from .adapters import AdapterError, detect_adapter, parse_any
from .adapters.pdf import parse_pdf_any, PdfAdapterError, detect_pdf_parser
from .db import db, init_db, make_fitid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATIC = os.path.join(ROOT, "static")

app = FastAPI(title="Chombi Finance Tracker")

# ---------- helpers ----------

def cat_map(con):
    return {r["id"]: dict(r) for r in con.execute("SELECT * FROM categories")}


def cat_by_key(con):
    return {r["key"]: dict(r) for r in con.execute("SELECT * FROM categories")}


def learned_map(con):
    return {r["cleaned"].lower(): r["category_key"] for r in con.execute("SELECT * FROM learned_rules")}


def ensure_account(con, name: str, bank: str = "", kind: str = "checking", exclude: bool = False) -> int:
    row = con.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = con.execute(
        "INSERT INTO accounts(name,bank,kind,exclude) VALUES(?,?,?,?)",
        (name, bank, kind, 1 if exclude else 0),
    )
    return cur.lastrowid


# ---------- models ----------

class TxnPatch(BaseModel):
    category: str | None = None      # category key
    description: str | None = None
    learn: bool = True               # create/update a learned rule from cleaned merchant
    apply_to_similar: bool = False   # also retag every same-merchant transaction


class CategoryIn(BaseModel):
    name: str
    kind: str = "expense"
    color: str = "#64748b"
    key: str | None = None


class CategoryPatch(BaseModel):
    hidden: bool | None = None
    name: str | None = None
    color: str | None = None


# ---------- lifecycle ----------

@app.on_event("startup")
def _startup():
    init_db(cat.DEFAULT_CATEGORIES)


# ---------- import ----------

def pdf_to_text(raw_bytes: bytes) -> str:
    """Extract text from a PDF using pypdf. Raises HTTPException on failure."""
    try:
        import pypdf
    except Exception:
        raise HTTPException(500, "PDF support unavailable (pypdf not installed)")
    try:
        rd = pypdf.PdfReader(io.BytesIO(raw_bytes))
        if getattr(rd, "is_encrypted", False):
            try:
                rd.decrypt("")
            except Exception:
                raise HTTPException(400, "PDF is password-protected")
        return "\n".join((p.extract_text() or "") for p in rd.pages)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Could not read PDF: {e}")


@app.post("/api/import")
async def import_csv(file: UploadFile = File(...), account: str = Form(...),
                     bank: str = Form(""), kind: str = Form("checking"),
                     exclude: bool = Form(False)):
    raw_bytes = await file.read()
    fname = file.filename or ""
    is_pdf = fname.lower().endswith(".pdf") or raw_bytes[:5] == b"%PDF-"
    if is_pdf:
        text = pdf_to_text(raw_bytes)
        try:
            txns, detected = parse_pdf_any(text, account)
        except PdfAdapterError as e:
            raise HTTPException(400, str(e))
    else:
        raw = raw_bytes.decode("utf-8-sig", errors="replace")
        try:
            txns, detected = parse_any(raw, account)
        except AdapterError as e:
            raise HTTPException(400, str(e))
    if not txns:
        raise HTTPException(400, "No transactions parsed from file")

    batch = uuid.uuid4().hex[:12]
    inserted = skipped = 0
    skipped_rows = []   # {date, amount, description, reason}
    seen = {}   # identity -> occurrence index within THIS batch (disambiguates identical rows)
    with db() as con:
        acct_id = ensure_account(con, account, bank or detected, kind, exclude)
        lm = learned_map(con)
        cats = cat_by_key(con)
        for t in txns:
            cleaned = cat.clean_merchant(t.description)
            _key = (t.date.isoformat(), round(float(t.amount), 2), " ".join((t.description or "").strip().lower().split()).rstrip("\\\"'` "))
            occ = seen.get(_key, 0)
            seen[_key] = occ + 1
            occ_dupe = occ > 0   # a same-identity row earlier in this very batch
            fid = make_fitid(account, t.date, t.amount, t.description, occ)
            ckey, src = cat.categorize(t.description, cleaned, lm, t.raw_type, t.amount)
            if ckey not in cats:
                ckey, src = "uncategorized", "uncategorized"
            cid = cats[ckey]["id"]
            try:
                con.execute(
                    """INSERT INTO transactions
                       (account_id,date,description,cleaned,amount,raw_type,category_id,
                        category_source,memo,fitid,import_batch)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (acct_id, t.date.isoformat(), t.description, cleaned, t.amount,
                     t.raw_type, cid, src, t.memo, fid, batch),
                )
                inserted += 1
            except Exception as exc:
                skipped += 1
                reason = "duplicate" if "UNIQUE" in str(exc).upper() else f"error: {exc}"
                skipped_rows.append({"date": t.date.isoformat(), "amount": round(float(t.amount), 2),
                                     "description": t.description, "reason": reason,
                                     "same_batch_identity": bool(occ_dupe)})
        con.execute("INSERT INTO imports(id,filename,account,bank,inserted,skipped) VALUES(?,?,?,?,?,?)",
                    (batch, file.filename, account, bank or detected, inserted, skipped))

    # Count identity groups that appear >1x in the source (the "looks like a dupe"
    # case). If any of those got skipped, it's worth flagging loudly.
    multi_identity = {k: v for k, v in seen.items() if v > 1}
    flagged = [r for r in skipped_rows if r.get("same_batch_identity")]
    warning = None
    if flagged:
        warning = (f"{len(flagged)} row(s) were skipped even though identical rows exist in "
                   f"this same file — check whether they are genuine repeats vs. true duplicates.")
    elif multi_identity:
        warning = (f"{len(multi_identity)} identity group(s) repeat within this file "
                   f"(same date+amount+description); all were inserted.")
    return {"ok": True, "batch": batch, "bank": detected, "account": account,
            "inserted": inserted, "skipped_duplicates": skipped, "parsed": len(txns),
            "skipped_detail": skipped_rows,
            "repeated_identities": len(multi_identity),
            "warning": warning}


class StatementAccount(BaseModel):
    name: str
    number: str = ""
    exclude: bool = False
    kind: str = "savings"
    txns: list[dict] = []


class StatementImport(BaseModel):
    filename: str = "statement.pdf"
    bank: str = "capital_one"
    accounts: list[StatementAccount] = []


@app.post("/api/import/statement")
def import_statement(body: StatementImport):
    """Import a pre-parsed multi-account statement (e.g. Capital One 360 PDF).

    Each sub-account becomes its own app account. Idempotent: re-importing the same
    statement is deduped on fitid, so nothing is duplicated.
    """
    results = []
    for acct in body.accounts:
        if not acct.txns:
            results.append({"account": acct.name, "inserted": 0, "skipped_duplicates": 0,
                            "parsed": 0, "skipped_no_txns": True})
            continue
        batch = uuid.uuid4().hex[:12]
        inserted = skipped = 0
        skipped_rows = []
        seen = {}
        with db() as con:
            acct_id = ensure_account(con, acct.name, body.bank, acct.kind, acct.exclude)
            lm = learned_map(con)
            cats = cat_by_key(con)
            for t in acct.txns:
                d = t.get("date")
                try:
                    d_obj = date.fromisoformat(d)
                except (ValueError, TypeError):
                    skipped += 1
                    skipped_rows.append({"date": str(d), "amount": t.get("amount"),
                                         "description": t.get("description", ""),
                                         "reason": "bad date", "same_batch_identity": False})
                    continue
                desc = t.get("description", "")
                amt = float(t.get("amount", 0))
                raw_type = t.get("raw_type", "")
                cleaned = cat.clean_merchant(desc)
                _key = (d_obj.isoformat(), round(amt, 2), " ".join((desc or "").strip().lower().split()).rstrip("\\\"'` "))
                occ = seen.get(_key, 0)
                seen[_key] = occ + 1
                occ_dupe = occ > 0
                fid = make_fitid(acct.name, d_obj, amt, desc, occ)
                ckey, src = cat.categorize(desc, cleaned, lm, raw_type, amt)
                if ckey not in cats:
                    ckey, src = "uncategorized", "uncategorized"
                cid = cats[ckey]["id"]
                try:
                    con.execute(
                        """INSERT INTO transactions
                           (account_id,date,description,cleaned,amount,raw_type,category_id,
                            category_source,memo,fitid,import_batch)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (acct_id, d_obj.isoformat(), desc, cleaned, amt,
                         raw_type, cid, src, "", fid, batch))
                    inserted += 1
                except Exception as exc:
                    skipped += 1
                    reason = "duplicate" if "UNIQUE" in str(exc).upper() else f"error: {exc}"
                    skipped_rows.append({"date": d_obj.isoformat(), "amount": round(amt, 2),
                                         "description": desc, "reason": reason,
                                         "same_batch_identity": bool(occ_dupe)})
            con.execute("INSERT INTO imports(id,filename,account,bank,inserted,skipped) VALUES(?,?,?,?,?,?)",
                        (batch, f"{body.filename}#{acct.name}", acct.name, body.bank, inserted, skipped))
        multi = sum(1 for v in seen.values() if v > 1)
        flagged = [r for r in skipped_rows if r.get("same_batch_identity")]
        results.append({"account": acct.name, "inserted": inserted,
                        "skipped_duplicates": skipped, "parsed": len(acct.txns),
                        "skipped_detail": skipped_rows, "repeated_identities": multi,
                        "warning": (f"{len(flagged)} skipped row(s) have identical rows in this file" if flagged
                                    else (f"{multi} identity group(s) repeat within this file" if multi else None))})
    total_in = sum(r["inserted"] for r in results)
    return {"ok": True, "total_inserted": total_in,
            "total_skipped": sum(r["skipped_duplicates"] for r in results), "results": results}


@app.post("/api/import/preview")
async def import_preview(file: UploadFile = File(...)):
    raw_bytes = await file.read()
    fname = file.filename or ""
    is_pdf = fname.lower().endswith(".pdf") or raw_bytes[:5] == b"%PDF-"
    if is_pdf:
        text = pdf_to_text(raw_bytes)
        parser, label = detect_pdf_parser(text)
        if parser is None:
            return {"detected": None, "error": "Unknown PDF format"}
        txns = parser(text, "preview")[:8]
        return {"detected": label, "bank": "pdf", "sample": [
            {"date": t.date.isoformat(), "description": t.description, "amount": t.amount,
             "cleaned": cat.clean_merchant(t.description),
             "suggested": cat.categorize(t.description, cat.clean_merchant(t.description))[0]}
            for t in txns]}
    raw = raw_bytes.decode("utf-8-sig", errors="replace")
    a = detect_adapter(raw)
    if a is None:
        return {"detected": None, "error": "Unknown format"}
    txns = a.parse(raw, "preview")[:8]
    return {"detected": a.label, "bank": a.name, "sample": [
        {"date": t.date.isoformat(), "description": t.description, "amount": t.amount,
         "cleaned": cat.clean_merchant(t.description),
         "suggested": cat.categorize(t.description, cat.clean_merchant(t.description))[0]}
        for t in txns]}


class AccountResolve(BaseModel):
    name: str
    bank: str = ""


def _norm_acct(s: str) -> str:
    """Normalize an account label for comparison: lowercase, drop punctuation,
    collapse whitespace, and drop common generic tokens."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(checking|checkings|savings|credit|card|acct|account|bank)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _similar(a: str, b: str) -> float:
    """0..1 similarity on normalized labels: token-overlap + difflib ratio."""
    na, nb = _norm_acct(a), _norm_acct(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    tok = len(ta & tb) / max(1, len(ta | tb))
    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    # token containment (one label is a prefix/subset of the other) is a strong signal
    contain = 1.0 if (ta <= tb or tb <= ta) else 0.0
    return max(tok, seq * 0.9, contain)


@app.post("/api/accounts/resolve")
def resolve_account(body: AccountResolve):
    """Given a proposed import account label, tell the UI whether it matches an
    existing account (exact or fuzzy) so a duplicate account isn't silently created."""
    name = (body.name or "").strip()
    with db() as con:
        rows = con.execute("SELECT id,name,bank,kind,exclude FROM accounts").fetchall()
    if not name:
        return {"input": name, "exact": None, "suggestions": []}
    exact = next((dict(r) for r in rows if r["name"].strip().lower() == name.lower()), None)
    scored = []
    for r in rows:
        sc = _similar(name, r["name"])
        # small bonus if same bank detected
        if body.bank and (r["bank"] or "").lower() == body.bank.lower():
            sc = min(1.0, sc + 0.05)
        if sc >= 0.55:
            scored.append({"id": r["id"], "name": r["name"], "bank": r["bank"],
                           "kind": r["kind"], "exclude": r["exclude"],
                           "score": round(sc, 3)})
    scored.sort(key=lambda x: -x["score"])
    return {"input": name, "exact": exact, "suggestions": scored[:5]}


# ---------- dashboard ----------

@app.get("/api/dashboard")
def dashboard(start: str | None = None, end: str | None = None,
              accounts: str | None = None):
    where, params = [], []
    ids = [int(x) for x in accounts.split(",") if x.strip().isdigit()] if accounts else []
    if ids:
        # explicit account selection: honor it and do NOT drop excluded accounts,
        # so picking a savings/bucket account actually shows its activity.
        where.append(f"t.account_id IN ({','.join('?' * len(ids))})"); params.extend(ids)
    else:
        # no explicit selection: exclude flagged accounts (e.g. internal buckets)
        where.append("ac.exclude = 0")
    if start:
        where.append("t.date >= ?"); params.append(start)
    if end:
        where.append("t.date <= ?"); params.append(end)
    w = " AND ".join(where)

    with db() as con:
        rows = con.execute(f"""
            SELECT t.date, t.amount, t.category_source,
                   c.key AS ckey, c.name AS cname, c.kind AS ckind, c.color AS ccolor,
                   c.hidden AS chidden,
                   ac.name AS account
            FROM transactions t
            JOIN categories c ON c.id = t.category_id
            JOIN accounts ac ON ac.id = t.account_id
            WHERE {w}
        """, params).fetchall()

    income = expense = 0.0
    by_cat = defaultdict(lambda: {"name": "", "color": "", "kind": "", "total": 0.0, "count": 0})
    by_month = defaultdict(lambda: {"income": 0.0, "expense": 0.0})
    by_account = defaultdict(lambda: {"income": 0.0, "expense": 0.0})
    uncategorized = 0
    uncategorized_amt = 0.0

    for r in rows:
        if r["ckind"] == "transfer" or r["chidden"]:
            continue
        amt = r["amount"]
        m = r["date"][:7]
        if amt >= 0:
            income += amt
            by_month[m]["income"] += amt
            by_account[r["account"]]["income"] += amt
        else:
            expense += -amt
            by_month[m]["expense"] += -amt
            by_account[r["account"]]["expense"] += -amt
        c = by_cat[r["ckey"]]
        c["name"], c["color"], c["kind"] = r["cname"], r["ccolor"], r["ckind"]
        c["hidden"] = r["chidden"]
        c["total"] += abs(amt)
        c["count"] += 1
        if r["ckey"] == "uncategorized" and amt < 0:
            uncategorized += 1
            uncategorized_amt += -amt

    cats_out = [{"key": k, **v} for k, v in by_cat.items()
                if v["kind"] != "transfer" and not v.get("hidden")]
    cats_out.sort(key=lambda x: -x["total"])
    months = [{"month": k, **v}
              for k, v in sorted(by_month.items(), key=lambda x: x[0])]

    # ---- grading ----
    grade = compute_grade(income, expense)
    with db() as con:
        all_income, all_expense = _totals_for(con)
        grade["all_time"] = compute_grade(all_income, all_expense)
        # trend: compare this window to the immediately preceding equal-length window
        trend = None
        if start and end:
            try:
                ds = date.fromisoformat(start); de = date.fromisoformat(end)
                span = (de - ds).days + 1
                prev_end = (ds - timedelta(days=1))
                prev_start = prev_end - timedelta(days=span - 1)
                pi, pe = _totals_for(con, prev_start.isoformat(), prev_end.isoformat())
                pg = compute_grade(pi, pe)
                if grade["rate"] is not None and pg["rate"] is not None:
                    diff = round(grade["rate"] - pg["rate"], 1)
                    # Clamp the displayed delta so extreme base rates don't produce absurd figures.
                    shown = max(-999.9, min(999.9, diff))
                    trend = {"direction": "up" if diff > 0.05 else ("down" if diff < -0.05 else "flat"),
                             "delta": shown, "prev_grade": pg["grade"], "prev_rate": pg["rate"]}
            except ValueError:
                trend = None
        grade["trend"] = trend

    return {
        "totals": {"income": round(income, 2), "expense": round(expense, 2),
                   "net": round(income - expense, 2)},
        "grade": grade,
        "categories": cats_out,
        "months": months,
        "accounts": [{"name": k, **v} for k, v in by_account.items()],
        "uncategorized": {"count": uncategorized, "amount": round(uncategorized_amt, 2)},
        "range": {"start": start, "end": end},
    }


# ---------- trends ----------

@app.get("/api/trends")
def trends(start: str | None = None, end: str | None = None,
           accounts: str | None = None, months: int = 12):
    """Per-category monthly series + 3-month averages, expense-focused.

    `months` limits how many of the most recent months are returned.
    Transfers and hidden categories are always excluded (not real spend).
    """
    where, params = ["c.kind != 'transfer'", "c.hidden = 0"], []
    ids = [int(x) for x in accounts.split(",") if x.strip().isdigit()] if accounts else []
    if ids:
        # explicit selection: show it even if the account is otherwise excluded
        where.append(f"t.account_id IN ({','.join('?' * len(ids))})"); params.extend(ids)
    else:
        where.insert(0, "ac.exclude = 0")
    if start:
        where.append("t.date >= ?"); params.append(start)
    if end:
        where.append("t.date <= ?"); params.append(end)
    w = " AND ".join(where)

    with db() as con:
        rows = con.execute(f"""
            SELECT t.date, t.amount, c.key ckey, c.name cname, c.color ccolor
            FROM transactions t
            JOIN categories c ON c.id = t.category_id
            JOIN accounts ac ON ac.id = t.account_id
            WHERE {w}
        """, params).fetchall()

    # month -> category -> expense; also track income per month
    cat_month = defaultdict(lambda: defaultdict(float))
    cat_meta: dict[str, dict] = {}
    month_income = defaultdict(float)
    month_expense = defaultdict(float)
    for r in rows:
        m = r["date"][:7]
        cat_meta[r["ckey"]] = {"name": r["cname"], "color": r["ccolor"]}
        amt = r["amount"]
        if amt >= 0:
            month_income[m] += amt
        else:
            cat_month[r["ckey"]][m] += -amt
            month_expense[m] += -amt

    all_months = sorted(set(list(month_income) + list(month_expense) + [
        m for cm in cat_month.values() for m in cm]))
    if months and len(all_months) > months:
        all_months = all_months[-months:]
    mset = set(all_months)

    cats = []
    for ck, per_month in cat_month.items():
        series = {m: round(per_month.get(m, 0.0), 2) for m in all_months}
        total = round(sum(series.values()), 2)
        active = [v for v in series.values() if v > 0]
        recent = all_months[-3:]
        avg3 = round(sum(per_month.get(m, 0.0) for m in recent) / max(len(recent), 1), 2)
        avg_all = round(sum(active) / len(active), 2) if active else 0.0
        cats.append({
            "key": ck, "name": cat_meta[ck]["name"], "color": cat_meta[ck]["color"],
            "total": total, "avg_3mo": avg3, "avg_active": avg_all,
            "series": series,
        })
    cats.sort(key=lambda x: -x["total"])

    month_series = [{
        "month": m,
        "income": round(month_income.get(m, 0.0), 2),
        "expense": round(month_expense.get(m, 0.0), 2),
        "net": round(month_income.get(m, 0.0) - month_expense.get(m, 0.0), 2),
    } for m in all_months]

    # 3-month averages of overall income/expense
    recent3 = all_months[-3:]
    avg_inc = round(sum(month_income.get(m, 0.0) for m in recent3) / max(len(recent3), 1), 2)
    avg_exp = round(sum(month_expense.get(m, 0.0) for m in recent3) / max(len(recent3), 1), 2)

    return {
        "months": all_months,
        "categories": cats,
        "month_series": month_series,
        "avg_3mo": {"income": avg_inc, "expense": avg_exp,
                     "net": round(avg_inc - avg_exp, 2)},
    }


# ---------- budgets ----------

class BudgetIn(BaseModel):
    category_key: str
    amount: float


@app.get("/api/budgets")
def get_budgets(month: str | None = None, accounts: str | None = None):
    """Monthly budget targets + actual spend for a given month (YYYY-MM).

    Defaults to the month of the latest transaction. Returns one row per
    expense category that either has a budget set OR had spend this month.
    """
    ids = [int(x) for x in accounts.split(",") if x.strip().isdigit()] if accounts else []
    with db() as con:
        if not month:
            mrow = con.execute("SELECT MAX(date) d FROM transactions").fetchone()
            month = (mrow["d"] or date.today().isoformat())[:7]
        where = ["c.kind = 'expense'", "c.hidden = 0", "substr(t.date,1,7) = ?"]
        params: list = [month]
        if ids:
            where.append(f"t.account_id IN ({','.join('?' * len(ids))})"); params.extend(ids)
        else:
            where.insert(0, "ac.exclude = 0")
        w = " AND ".join(where)
        spent = {r["ckey"]: -r["s"] for r in con.execute(f"""
            SELECT c.key ckey, SUM(t.amount) s
            FROM transactions t
            JOIN categories c ON c.id = t.category_id
            JOIN accounts ac ON ac.id = t.account_id
            WHERE {w}
            GROUP BY c.key
        """, params).fetchall()}
        targets = {r["category_key"]: r["amount"]
                   for r in con.execute("SELECT * FROM budgets").fetchall()
                   if r["amount"] and r["amount"] > 0}
        meta = {r["key"]: dict(r) for r in con.execute(
            "SELECT key,name,color FROM categories WHERE kind='expense' AND hidden=0").fetchall()}

    keys = set(targets) | set(spent)
    rows = []
    for k in keys:
        m = meta.get(k, {})
        tgt = round(targets.get(k, 0.0), 2)
        sp = round(spent.get(k, 0.0), 2)
        pct = round(sp / tgt * 100, 1) if tgt > 0 else None
        rows.append({
            "key": k, "name": m.get("name", k), "color": m.get("color", "#64748b"),
            "budget": tgt, "spent": sp, "remaining": round(tgt - sp, 2),
            "pct": pct,
        })
    # budgeted first (by pct spent desc), then the rest by spend desc
    rows.sort(key=lambda r: (r["budget"] <= 0, -(r["pct"] or 0), -r["spent"]))

    total_budget = round(sum(r["budget"] for r in rows), 2)
    # Spend in *budgeted* categories only (drives the budget bars).
    total_spent = round(sum(r["spent"] for r in rows if r["budget"] > 0), 2)
    # ALL expense spend this month (budgeted or not) — the honest "usage" figure.
    total_spent_all = round(sum(r["spent"] for r in rows), 2)
    return {
        "month": month,
        "rows": rows,
        "totals": {"budget": total_budget, "spent": total_spent,
                   "spent_all": total_spent_all,
                   "spent_unbudgeted": round(total_spent_all - total_spent, 2)},
    }


@app.post("/api/budgets")
def set_budget(body: BudgetIn):
    """Set (or clear) a category's monthly budget. amount<=0 clears it."""
    with db() as con:
        exists = con.execute("SELECT 1 FROM categories WHERE key=?", (body.category_key,)).fetchone()
        if not exists:
            raise HTTPException(404, f"unknown category: {body.category_key}")
        if body.amount and body.amount > 0:
            con.execute("""INSERT INTO budgets(category_key,amount,updated_at)
                VALUES(?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(category_key) DO UPDATE SET amount=excluded.amount,
                updated_at=CURRENT_TIMESTAMP""", (body.category_key, round(body.amount, 2)))
        else:
            con.execute("DELETE FROM budgets WHERE category_key=?", (body.category_key,))
    return {"ok": True, "category_key": body.category_key,
            "amount": round(body.amount, 2) if body.amount and body.amount > 0 else 0}


# ---------- bills (recurring payments + paid/due tracking) ----------

_CARD_RE = re.compile(
    r"(CARDPYMT|CARD ?PYMT|WEB PMT|PAYMENT THANK YOU|CHASE|GEMINI|MASTERCARD|VISA)", re.I)


def _detect_bill_candidates(con):
    """Scan outflow history and propose recurring bills.

    A candidate recurs across >=3 distinct months at a stable amount (low
    coefficient of variation). Rule-based, no LLM. Nothing is written here;
    callers decide which candidates to turn into real bills.
    """
    import statistics
    rows = con.execute("""
        SELECT cleaned, date, amount FROM transactions
        WHERE amount < 0 AND cleaned IS NOT NULL AND cleaned != ''
        ORDER BY cleaned, date""").fetchall()
    groups: dict = {}
    for r in rows:
        groups.setdefault(r["cleaned"], []).append(r)
    out = []
    for cleaned, rs in groups.items():
        months = {r["date"][:7] for r in rs}
        if len(months) < 3:
            continue
        amts = [-r["amount"] for r in rs]
        avg = statistics.mean(amts)
        if avg <= 0:
            continue
        sd = statistics.pstdev(amts) if len(amts) > 1 else 0.0
        cv = sd / avg if avg else 9.0
        if cv > 0.35:            # amount too variable to be a fixed bill
            continue
        days = [int(r["date"][8:10]) for r in rs]
        out.append({
            "match_cleaned": cleaned,
            "name": cleaned.title(),
            "expected_amount": round(avg, 2),
            "due_day": round(statistics.median(days)),
            "kind": "card" if _CARD_RE.search(cleaned) else "bill",
            "months": len(months),
            "occurrences": len(rs),
            "last_date": max(r["date"] for r in rs),
        })
    out.sort(key=lambda c: (-c["months"], -c["occurrences"]))
    return out


def _bill_status(con, bill, month):
    """Compute this month's paid/due/overdue state for one bill.

    Matches a txn whose cleaned merchant *contains* the bill's match key
    (case-insensitive) — merchants are often fragmented across variants
    (NETFLIX 1 / NETFLIX COM ...), so substring matching is intentional.
    Returns paid (with date+amount) or a due-state derived from due_day.
    """
    key = (bill["match_cleaned"] or "").lower()
    row = con.execute("""
        SELECT t.date, t.amount FROM transactions t
        WHERE lower(t.cleaned) LIKE ? AND substr(t.date,1,7) = ? AND t.amount < 0
        ORDER BY t.date DESC LIMIT 1""", (f"%{key}%", month)).fetchone()
    today = date.today()
    status, paid_date, paid_amount = "due", None, None
    manual = None
    try:
        manual = bill["manual_paid_month"]
    except (KeyError, IndexError):
        manual = None
    if manual == month:
        # user hand-marked this month paid (cash / other account / early)
        return {"status": "paid", "paid_date": None, "paid_amount": bill["expected_amount"],
                "variance": None, "manual": True}
    if row:
        status, paid_date, paid_amount = "paid", row["date"], round(-row["amount"], 2)
    else:
        day = bill["due_day"] or 1
        try:
            due = date(int(month[:4]), int(month[5:7]), day)
        except ValueError:
            due = date(int(month[:4]), int(month[5:7]), 28)
        if today.strftime("%Y-%m") > month:
            status = "missed"          # month is fully in the past, never posted
        elif today >= due + timedelta(days=3):
            status = "overdue"
        elif today >= due - timedelta(days=5):
            status = "due"             # inside expected window
        else:
            status = "upcoming"
    return {
        "status": status,
        "paid_date": paid_date,
        "paid_amount": paid_amount,
        "variance": round(paid_amount - bill["expected_amount"], 2) if paid_amount is not None else None,
        "manual": False,
    }


class MarkPaidIn(BaseModel):
    month: str | None = None      # 'YYYY-MM'; defaults to current month
    paid: bool = True             # True = mark paid, False = undo


@app.post("/api/bills/{bill_id}/paid")
def mark_bill_paid(bill_id: int, body: MarkPaidIn):
    """Manually mark a bill paid for a month (or undo). Overrides auto-detection
    for that month only; a matching transaction still shows its real amount/date
    on other months."""
    month = body.month or date.today().strftime("%Y-%m")
    with db() as con:
        if not con.execute("SELECT 1 FROM bills WHERE id=?", (bill_id,)).fetchone():
            raise HTTPException(404, "not found")
        con.execute("UPDATE bills SET manual_paid_month=? WHERE id=?",
                    (month if body.paid else None, bill_id))
    return {"ok": True, "manual_paid_month": month if body.paid else None}


class BillIn(BaseModel):
    name: str | None = None
    match_cleaned: str | None = None
    expected_amount: float | None = None
    due_day: int | None = None
    kind: str | None = None
    category_key: str | None = None
    active: bool | None = None


@app.get("/api/bills")
def get_bills(month: str | None = None, include_inactive: bool = False):
    """Confirmed bills with this month's paid/due status."""
    with db() as con:
        if not month:
            mrow = con.execute("SELECT MAX(date) d FROM transactions").fetchone()
            month = (mrow["d"] or date.today().isoformat())[:7]
        where = "" if include_inactive else "WHERE active = 1"
        bills = con.execute(f"SELECT * FROM bills {where} ORDER BY kind, due_day, name").fetchall()
        out = []
        for b in bills:
            st = _bill_status(con, b, month)
            d = dict(b); d.update(st)
            out.append(d)
    counts = {
        "paid": sum(1 for r in out if r["status"] == "paid"),
        "due": sum(1 for r in out if r["status"] in ("due", "upcoming")),
        "overdue": sum(1 for r in out if r["status"] in ("overdue", "missed")),
        "total": len(out),
        "expected": round(sum(r.get("expected_amount") or 0 for r in out), 2),
        "paid_amount": round(sum((r.get("paid_amount") or 0) for r in out if r["status"] == "paid"), 2),
        "left": round(sum((r.get("expected_amount") or 0) for r in out if r["status"] != "paid"), 2),
    }
    # surface overdue first, then due, then paid
    order = {"overdue": 0, "missed": 1, "due": 2, "upcoming": 3, "paid": 4}
    out.sort(key=lambda r: (order.get(r["status"], 9), r["due_day"] or 0))
    return {"month": month, "rows": out, "counts": counts}


@app.get("/api/bills/candidates")
def bill_candidates():
    """Propose recurring bills from history, excluding ones already tracked."""
    with db() as con:
        tracked = {r["match_cleaned"] for r in con.execute("SELECT match_cleaned FROM bills").fetchall()}
        cands = [c for c in _detect_bill_candidates(con)
                 if c["match_cleaned"] not in tracked and c["kind"] != "card"]
    return {"candidates": cands}


# Common bill categories, in display order. key -> label.
_BILL_GROUPS = [
    ("housing", "Housing"),
    ("loan_payment", "Loans"),
    ("utilities", "Utilities"),
    ("insurance", "Insurance"),
    ("subscriptions", "Subscriptions"),
    ("mobile", "Mobile"),
    ("daycare", "Daycare"),
    ("kids_activities", "Kids activities"),
    ("college_savings", "College Savings"),
    ("health", "Health"),
    ("card", "Card payments"),
    ("other", "Other"),
]


@app.get("/api/bill-categories")
def bill_categories():
    """The category vocabulary available for grouping bills (for the add modal)."""
    return {"categories": [{"key": k, "name": n} for k, n in _BILL_GROUPS]}


@app.get("/api/bills/grouped")
def bills_grouped(month: str | None = None, include_inactive: bool = False):
    """Bills grouped by category, each group with its own paid/due counts.

    Groups appear in _BILL_GROUPS order; any unknown/empty keys fall under
    'other'. Within a group, bills are ordered by status then due_day.
    """
    base = get_bills(month=month, include_inactive=include_inactive)
    rows = base["rows"]
    labels = dict(_BILL_GROUPS)
    order = {k: i for i, (k, _) in enumerate(_BILL_GROUPS)}
    buckets: dict[str, list] = {}
    for r in rows:
        key = (r.get("category_key") or "").strip() or "other"
        if key not in labels:
            key = "other"
        buckets.setdefault(key, []).append(r)
    groups = []
    for key, items in sorted(buckets.items(), key=lambda kv: order.get(kv[0], 99)):
        groups.append({
            "key": key,
            "name": labels.get(key, key.title()),
            "bills": items,
            "counts": {
                "paid": sum(1 for r in items if r["status"] == "paid"),
                "due": sum(1 for r in items if r["status"] in ("due", "upcoming")),
                "overdue": sum(1 for r in items if r["status"] in ("overdue", "missed")),
                "total": len(items),
                "expected": round(sum(r.get("expected_amount") or 0 for r in items), 2),
            },
        })
    return {"month": base["month"], "groups": groups, "counts": base["counts"]}


@app.post("/api/bills")
def add_bill(body: BillIn):
    key = (body.match_cleaned or body.name or "").strip().lower()
    if not key:
        raise HTTPException(400, "name or match_cleaned required")
    with db() as con:
        con.execute("""INSERT INTO bills(name,match_cleaned,expected_amount,due_day,kind,category_key,active)
            VALUES(?,?,?,?,?,?,1)
            ON CONFLICT(match_cleaned) DO UPDATE SET
              name=excluded.name, expected_amount=excluded.expected_amount,
              due_day=excluded.due_day, kind=excluded.kind,
              category_key=excluded.category_key, active=1""",
            (body.name or key.title(), key, body.expected_amount or 0,
             body.due_day or 0, body.kind or "bill", body.category_key or ""))
    return {"ok": True, "match_cleaned": key}


@app.patch("/api/bills/{bill_id}")
def patch_bill(bill_id: int, body: BillIn):
    with db() as con:
        if not con.execute("SELECT 1 FROM bills WHERE id=?", (bill_id,)).fetchone():
            raise HTTPException(404, "not found")
        sets, params = [], []
        if body.name is not None:            sets.append("name=?");            params.append(body.name)
        if body.expected_amount is not None: sets.append("expected_amount=?"); params.append(body.expected_amount)
        if body.due_day is not None:         sets.append("due_day=?");         params.append(body.due_day)
        if body.kind is not None:            sets.append("kind=?");            params.append(body.kind)
        if body.category_key is not None:    sets.append("category_key=?");    params.append(body.category_key)
        if body.active is not None:          sets.append("active=?");          params.append(1 if body.active else 0)
        if sets:
            con.execute(f"UPDATE bills SET {', '.join(sets)} WHERE id=?", (*params, bill_id))
    return {"ok": True}


@app.delete("/api/bills/{bill_id}")
def delete_bill(bill_id: int):
    with db() as con:
        con.execute("DELETE FROM bills WHERE id=?", (bill_id,))
    return {"ok": True}


# ---------- transactions ----------

@app.get("/api/transactions")
def list_txns(start: str | None = None, end: str | None = None,
              category: str | None = None, account: str | None = None,
              accounts: str | None = None,
              q: str | None = None, min_amt: float | None = None, max_amt: float | None = None,
              source: str | None = None, include_hidden: bool = False,
              limit: int = 500, offset: int = 0,
              sort: str = "date", order: str = "desc"):
    where, params = [], []
    if not include_hidden and not category:
        # hide hidden categories unless the user is explicitly filtering on one
        where.append("c.hidden = 0")
    if start:
        where.append("t.date >= ?"); params.append(start)
    if end:
        where.append("t.date <= ?"); params.append(end)
    if category:
        keys = category.split(",")
        where.append(f"c.key IN ({','.join('?' * len(keys))})"); params.extend(keys)
    if account:
        where.append("ac.name = ?"); params.append(account)
    if accounts:
        names = [n for n in accounts.split(",") if n.strip()]
        if names:
            where.append(f"ac.name IN ({','.join('?' * len(names))})"); params.extend(names)
    if q:
        where.append("(t.description LIKE ? OR t.cleaned LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if min_amt is not None:
        where.append("t.amount >= ?"); params.append(min_amt)
    if max_amt is not None:
        where.append("t.amount <= ?"); params.append(max_amt)
    if source:
        where.append("t.category_source = ?"); params.append(source)

    w = ("WHERE " + " AND ".join(where)) if where else ""
    col = {"date": "t.date", "amount": "t.amount", "description": "t.description"}.get(sort, "t.date")
    direction = "ASC" if order.lower() == "asc" else "DESC"

    with db() as con:
        total = con.execute(f"""SELECT COUNT(*) n FROM transactions t
            JOIN categories c ON c.id=t.category_id
            JOIN accounts ac ON ac.id=t.account_id {w}""", params).fetchone()["n"]
        rows = con.execute(f"""
            SELECT t.id, t.date, t.description, t.cleaned, t.amount, t.raw_type,
                   t.category_source, t.memo, c.key ckey, c.name cname, c.color ccolor,
                   ac.name account
            FROM transactions t
            JOIN categories c ON c.id=t.category_id
            JOIN accounts ac ON ac.id=t.account_id
            {w} ORDER BY {col} {direction}, t.id DESC LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()
    return {"total": total, "items": [dict(r) for r in rows]}


@app.get("/api/transactions/{txn_id}/similar")
def similar_txns(txn_id: int):
    """How many OTHER transactions share this transaction's cleaned merchant,
    and what categories are they currently in. Used to preview bulk-tagging."""
    with db() as con:
        t = con.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
        if not t:
            raise HTTPException(404, "not found")
        cleaned = (t["cleaned"] or cat.clean_merchant(t["description"])).lower()
        rows = con.execute("""
            SELECT t2.id, t2.date, t2.amount, t2.description, c2.key ckey, c2.name cname
            FROM transactions t2 JOIN categories c2 ON c2.id=t2.category_id
            WHERE lower(t2.cleaned)=? AND t2.id!=?
            ORDER BY t2.date DESC
        """, (cleaned, txn_id)).fetchall()
    return {
        "merchant": cleaned,
        "count": len(rows),
        "items": [dict(r) for r in rows[:50]],
    }


@app.patch("/api/transactions/{txn_id}")
def patch_txn(txn_id: int, body: TxnPatch, apply_to_merchant: bool = Query(False)):
    with db() as con:
        t = con.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
        if not t:
            raise HTTPException(404, "not found")
        ck = body.category
        cid = None
        if ck:
            c = con.execute("SELECT id,key FROM categories WHERE key=?", (ck,)).fetchone()
            if not c:
                raise HTTPException(400, f"unknown category {ck}")
            cid = c["id"]
        applied = 0
        if body.description is not None:
            con.execute("UPDATE transactions SET description=? WHERE id=?", (body.description, txn_id))
        if cid:
            con.execute("UPDATE transactions SET category_id=?, category_source='manual' WHERE id=?",
                        (cid, txn_id))
            applied = 1
            if body.learn and ck:
                cleaned = (t["cleaned"] or cat.clean_merchant(t["description"])).lower()
                if cleaned:
                    con.execute("""INSERT INTO learned_rules(cleaned,category_key,hits,updated_at)
                        VALUES(?,?,1,CURRENT_TIMESTAMP)
                        ON CONFLICT(cleaned) DO UPDATE SET category_key=excluded.category_key,
                        hits=hits+1, updated_at=CURRENT_TIMESTAMP""", (cleaned, ck))

                    if body.apply_to_similar:
                        # explicitly applied to ALL same-merchant rows (any current category)
                        others = con.execute("""
                            SELECT t2.id FROM transactions t2
                            WHERE lower(t2.cleaned)=? AND t2.id!=?""",
                            (cleaned, txn_id)).fetchall()
                    else:
                        # default: only sweep up other uncategorized rows of the same merchant
                        others = con.execute("""
                            SELECT t2.id FROM transactions t2
                            JOIN categories c2 ON c2.id=t2.category_id
                            WHERE lower(t2.cleaned)=? AND t2.id!=?
                              AND c2.key IN ('uncategorized')""", (cleaned, txn_id)).fetchall()
                    for o in others:
                        con.execute("""UPDATE transactions SET category_id=?, category_source='learned'
                                       WHERE id=?""", (cid, o["id"]))
                        applied += 1
        row = con.execute("""SELECT t.id,t.date,t.description,t.amount,t.category_source,c.key ckey,
                             c.name cname FROM transactions t JOIN categories c ON c.id=t.category_id
                             WHERE t.id=?""", (txn_id,)).fetchone()
    return {"ok": True, "txn": dict(row), "applied": applied}


class BulkCategoryIn(BaseModel):
    ids: list[int]
    category: str


@app.post("/api/transactions/bulk-category")
def bulk_category(body: BulkCategoryIn):
    with db() as con:
        c = con.execute("SELECT id FROM categories WHERE key=?", (body.category,)).fetchone()
        if not c:
            raise HTTPException(400, "unknown category")
        n = 0
        for i in body.ids:
            cur = con.execute("UPDATE transactions SET category_id=?, category_source='manual' WHERE id=?",
                              (c["id"], i))
            n += cur.rowcount
    return {"ok": True, "updated": n}


# ---------- categories ----------

@app.get("/api/categories")
def get_categories():
    with db() as con:
        rows = con.execute("""SELECT c.*, COUNT(t.id) cnt FROM categories c
            LEFT JOIN transactions t ON t.category_id=c.id
            GROUP BY c.id ORDER BY c.kind DESC, c.name""").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/categories")
def add_category(body: CategoryIn):
    key = body.key or body.name.lower().strip().replace(" ", "_").replace("/", "_")
    with db() as con:
        try:
            con.execute("INSERT INTO categories(key,name,kind,color,is_custom) VALUES(?,?,?,?,1)",
                        (key, body.name, body.kind, body.color))
        except Exception:
            raise HTTPException(400, "category already exists")
    return {"ok": True, "key": key}


@app.patch("/api/categories/{key}")
def patch_category(key: str, body: CategoryPatch):
    with db() as con:
        row = con.execute("SELECT id FROM categories WHERE key=?", (key,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        if body.hidden is not None:
            con.execute("UPDATE categories SET hidden=? WHERE id=?",
                        (1 if body.hidden else 0, row["id"]))
        if body.name:
            con.execute("UPDATE categories SET name=? WHERE id=?", (body.name, row["id"]))
        if body.color:
            con.execute("UPDATE categories SET color=? WHERE id=?", (body.color, row["id"]))
    return {"ok": True}


@app.delete("/api/categories/{key}")
def delete_category(key: str):
    with db() as con:
        row = con.execute("SELECT id,is_custom FROM categories WHERE key=?", (key,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        if not row["is_custom"]:
            raise HTTPException(400, "cannot delete built-in category")
        unc = con.execute("SELECT id FROM categories WHERE key='uncategorized'").fetchone()["id"]
        con.execute("UPDATE transactions SET category_id=?, category_source='uncategorized' WHERE category_id=?",
                    (unc, row["id"]))
        con.execute("DELETE FROM learned_rules WHERE category_key=?", (key,))
        con.execute("DELETE FROM categories WHERE id=?", (row["id"],))
    return {"ok": True}


# ---------- accounts / rules / misc ----------

@app.get("/api/savings")
def get_savings(accounts: str | None = None):
    """Savings/reserve accounts with deposits, withdrawals, interest and net flows.

    Savings accounts are separated from the cash-flow dashboard: money moved in or
    out is a transfer, not income/expense. This endpoint summarises each savings
    account independently.
    """
    ids = [int(x) for x in accounts.split(",") if x.strip().isdigit()] if accounts else []
    where = "WHERE a.kind='savings'"
    params: list = []
    if ids:
        where += " AND a.id IN (%s)" % ",".join("?" * len(ids))
        params += ids
    with db() as con:
        accts = con.execute(
            f"SELECT a.id, a.name, a.bank, a.exclude, a.balance, a.balance_date FROM accounts a {where} ORDER BY a.name",
            params,
        ).fetchall()
        out = []
        t_in = t_out = t_int = 0.0
        for a in accts:
            txns = con.execute(
                """SELECT t.date, t.description, t.amount, c.key cat_key
                   FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
                   WHERE t.account_id=? ORDER BY t.date""",
                (a["id"],),
            ).fetchall()
            deposits = sum(t["amount"] for t in txns if t["amount"] > 0 and t["cat_key"] != "savings_interest")
            withdrawals = sum(-t["amount"] for t in txns if t["amount"] < 0)
            interest = sum(t["amount"] for t in txns if t["cat_key"] == "savings_interest" or
                           (t["amount"] > 0 and "interest" in (t["description"] or "").lower()))
            net = deposits + interest - withdrawals
            # current balance = stored anchor + activity after the anchor date
            balance = None
            if a["balance"] is not None:
                bdate = a["balance_date"] or ""
                post = sum(t["amount"] for t in txns if (t["date"] or "") > bdate)
                balance = round(a["balance"] + post, 2)
            t_in += deposits; t_out += withdrawals; t_int += interest
            out.append({
                "id": a["id"], "name": a["name"], "bank": a["bank"],
                "deposits": round(deposits, 2),
                "withdrawals": round(withdrawals, 2),
                "interest": round(interest, 2),
                "net": round(net, 2),
                "balance": balance,
                "balance_date": a["balance_date"],
                "count": len(txns),
                "last_date": txns[-1]["date"] if txns else None,
                "series": [dict(t) for t in txns],
            })
    return {
        "accounts": out,
        "totals": {
            "deposits": round(t_in, 2),
            "withdrawals": round(t_out, 2),
            "interest": round(t_int, 2),
            "net": round(t_in + t_int - t_out, 2),
            "balance": round(sum(a["balance"] for a in out if a["balance"] is not None), 2),
            "count": len(out),
        },
    }


@app.get("/api/accounts")
def get_accounts():
    with db() as con:
        rows = con.execute("""SELECT a.*, COUNT(t.id) cnt, MIN(t.date) first_date,
            MAX(t.date) last_date FROM accounts a
            LEFT JOIN transactions t ON t.account_id=a.id GROUP BY a.id ORDER BY a.name""").fetchall()
    return [dict(r) for r in rows]


@app.patch("/api/accounts/{acct_id}")
def patch_account(acct_id: int, exclude: bool = None, name: str = None, kind: str = None):
    with db() as con:
        if exclude is not None:
            con.execute("UPDATE accounts SET exclude=? WHERE id=?", (1 if exclude else 0, acct_id))
        if name:
            con.execute("UPDATE accounts SET name=? WHERE id=?", (name, acct_id))
        if kind:
            if kind not in ("checking", "savings", "credit"):
                raise HTTPException(400, "kind must be checking|savings|credit")
            con.execute("UPDATE accounts SET kind=? WHERE id=?", (kind, acct_id))
    return {"ok": True}


class BalanceBody(BaseModel):
    balance: float
    balance_date: str | None = None


@app.post("/api/accounts/{acct_id}/balance")
def set_account_balance(acct_id: int, body: BalanceBody):
    from datetime import date as _date
    bdate = body.balance_date or _date.today().isoformat()
    with db() as con:
        cur = con.execute("UPDATE accounts SET balance=?, balance_date=? WHERE id=?",
                          (body.balance, bdate, acct_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "account not found")
    return {"ok": True, "balance": body.balance, "balance_date": bdate}


@app.get("/api/rules")
def get_rules():
    with db() as con:
        rows = con.execute("SELECT * FROM learned_rules ORDER BY updated_at DESC LIMIT 500").fetchall()
    return [dict(r) for r in rows]


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int):
    with db() as con:
        con.execute("DELETE FROM learned_rules WHERE id=?", (rule_id,))
    return {"ok": True}


@app.get("/api/imports")
def get_imports():
    with db() as con:
        rows = con.execute("SELECT * FROM imports ORDER BY created_at DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/recategorize")
def recategorize():
    """Re-run the rules engine over non-manual transactions."""
    with db() as con:
        lm = learned_map(con); cats = cat_by_key(con)
        rows = con.execute("""SELECT t.id,t.description,t.cleaned,t.raw_type,t.amount,t.category_source,
            c.key ckey FROM transactions t JOIN categories c ON c.id=t.category_id
            WHERE t.category_source != 'manual'""").fetchall()
        n = 0
        for r in rows:
            # recompute the cleaned merchant too: cleaning rules may have improved since import
            cleaned = cat.clean_merchant(r["description"]) or r["cleaned"]
            ck, src = cat.categorize(r["description"], cleaned, lm, r["raw_type"], r["amount"])
            if ck not in cats:
                ck, src = "uncategorized", "uncategorized"
            if ck != r["ckey"] or cleaned != r["cleaned"]:
                con.execute("UPDATE transactions SET category_id=?, category_source=?, cleaned=? WHERE id=?",
                            (cats[ck]["id"], src, cleaned, r["id"]))
                if ck != r["ckey"]:
                    n += 1
    return {"ok": True, "updated": n}


# ---------- grading ----------

GRADE_SCALE = [
    (0.40, "A+", "#16a34a"),
    (0.30, "A",  "#22c55e"),
    (0.20, "B",  "#84cc16"),
    (0.10, "C",  "#eab308"),
    (0.00, "D",  "#f97316"),
]
GRADE_F = ("F", "#ef4444")


def compute_grade(income: float, expense: float) -> dict:
    """Grade a period by savings rate = (income - expense) / income.

    - No income AND no expense -> None/N/A (empty period)
    - No income but there IS spending -> F (you spent with nothing coming in)
    - Negative savings rate -> F
    """
    if income <= 0:
        if expense <= 0:
            return {"grade": "N/A", "color": "#64748b", "rate": None,
                    "net": 0.0, "income": 0.0, "expense": 0.0}
        return {"grade": GRADE_F[0], "color": GRADE_F[1], "rate": None,
                "net": round(income - expense, 2), "income": round(income, 2),
                "expense": round(expense, 2)}
    rate = (income - expense) / income
    for threshold, letter, color in GRADE_SCALE:
        if rate >= threshold:
            return {"grade": letter, "color": color, "rate": round(rate * 100, 1),
                    "net": round(income - expense, 2), "income": round(income, 2),
                    "expense": round(expense, 2)}
    return {"grade": GRADE_F[0], "color": GRADE_F[1], "rate": round(rate * 100, 1),
            "net": round(income - expense, 2), "income": round(income, 2),
            "expense": round(expense, 2)}


def _totals_for(con, start=None, end=None, exclude_zero_income=True) -> tuple[float, float]:
    """Return (income, expense) for a date window, excluding transfers,
    hidden categories, and excluded accounts."""
    where, params = ["ac.exclude = 0", "c.kind != 'transfer'", "c.hidden = 0"], []
    if start:
        where.append("t.date >= ?"); params.append(start)
    if end:
        where.append("t.date <= ?"); params.append(end)
    w = " AND ".join(where)
    rows = con.execute(f"""
        SELECT t.amount, c.kind FROM transactions t
        JOIN categories c ON c.id = t.category_id
        JOIN accounts ac ON ac.id = t.account_id
        WHERE {w}
    """, params).fetchall()
    income = sum(r["amount"] for r in rows if r["amount"] > 0)
    expense = sum(-r["amount"] for r in rows if r["amount"] < 0)
    return round(income, 2), round(expense, 2)


# ---------- crypto ----------

_CG = "https://api.coingecko.com/api/v3"
_UA = {"User-Agent": "Mozilla/5.0 (LFFD/1.0) FinanceTracker", "Accept": "application/json"}
_cache: dict = {}


def _http_get(url: str, timeout: int = 12, headers: dict | None = None) -> bytes:
    ctx = None
    try:
        import ssl as _ssl
        import certifi
        ctx = _ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = None
    req = urllib.request.Request(url, headers=headers or _UA)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def _cached(key: str, ttl: int, fn):
    """Tiny in-process TTL cache so rapid refreshes don't hammer the API."""
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def _cg(path: str, params: dict | None = None):
    q = urllib.parse.urlencode(params or {})
    url = f"{_CG}{path}" + (f"?{q}" if q else "")
    return json.loads(_http_get(url))


@app.get("/api/crypto/ohlc")
def crypto_ohlc(id: str = "bitcoin", days: int = 7, vs: str = "usd"):
    """OHLC candles for candlestick view. CoinGecko allows 1/7/14/30/90/180/365."""
    allowed = [1, 7, 14, 30, 90, 180, 365]
    d = min(allowed, key=lambda a: abs(a - days))

    def fetch():
        return _cg(f"/coins/{urllib.parse.quote(id)}/ohlc",
                   {"vs_currency": vs, "days": d})
    ttl = 120 if d <= 1 else 300
    data = _cached(f"ohlc:{id}:{d}:{vs}", ttl, fetch)
    candles = [[int(c[0]), c[1], c[2], c[3], c[4]] for c in (data or [])]
    return {"id": id, "days": d, "vs": vs, "candles": candles,
            "fetched_at": int(time.time())}


@app.get("/api/crypto/search")
def crypto_search(q: str = ""):
    """Search any coin by name/symbol via CoinGecko /search."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"results": []}

    def fetch():
        return _cg("/search", {"query": q})
    try:
        data = _cached(f"search:{q.lower()}", 300, fetch)
    except Exception:
        return {"results": []}
    out = []
    for c in (data.get("coins") or [])[:12]:
        out.append({
            "id": c.get("id"), "name": c.get("name"),
            "symbol": (c.get("symbol") or "").upper(),
            "rank": c.get("market_cap_rank"),
            "thumb": c.get("thumb"),
        })
    # exact/prefix symbols first, then by rank
    ql = q.lower()
    out.sort(key=lambda c: (0 if c["symbol"].lower() == ql or c["name"].lower() == ql else 1,
                            c["rank"] if c["rank"] else 10**9))
    return {"results": out[:10]}


@app.get("/api/crypto/markets")
def crypto_markets(per_page: int = 10, vs: str = "usd", ids: str = ""):
    """Top coins by market cap (default top 10), or specific coins via ?ids=a,b,c."""
    ids = (ids or "").strip()
    def fetch():
        params = {
            "vs_currency": vs, "sparkline": "true",
            "price_change_percentage": "1h,24h,7d",
        }
        if ids:
            params["ids"] = ids
            params["order"] = "market_cap_desc"
        else:
            params["order"] = "market_cap_desc"
            params["per_page"] = per_page
            params["page"] = 1
        return _cg("/coins/markets", params)
    cache_key = f"markets:{ids or per_page}:{vs}"
    data = _cached(cache_key, 60, fetch)
    out = []
    for c in data:
        out.append({
            "id": c.get("id"), "symbol": (c.get("symbol") or "").upper(),
            "name": c.get("name"), "image": c.get("image"),
            "price": c.get("current_price"),
            "market_cap": c.get("market_cap"), "rank": c.get("market_cap_rank"),
            "chg_24h": c.get("price_change_percentage_24h"),
            "chg_7d": c.get("price_change_percentage_7d_in_currency"),
            "high_24h": c.get("high_24h"), "low_24h": c.get("low_24h"),
            "spark": (c.get("sparkline_in_7d") or {}).get("price") or [],
        })
    return {"coins": out, "vs": vs, "fetched_at": int(time.time())}


@app.get("/api/crypto/chart")
def crypto_chart(id: str = "bitcoin", days: int = 7, vs: str = "usd"):
    """Price series for a coin. days: 1|7|30|90|365|max."""
    def fetch():
        return _cg(f"/coins/{urllib.parse.quote(id)}/market_chart",
                   {"vs_currency": vs, "days": days})
    ttl = 60 if days <= 1 else 300
    data = _cached(f"chart:{id}:{days}:{vs}", ttl, fetch)
    prices = [[int(p[0]), p[1]] for p in (data.get("prices") or [])]
    return {"id": id, "days": days, "vs": vs, "prices": prices,
            "fetched_at": int(time.time())}


# ---------- crypto news (RSS, no key) ----------

_NEWS_FEEDS = {
    "crypto": [
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("Cointelegraph", "https://cointelegraph.com/rss"),
        ("Decrypt", "https://decrypt.co/feed"),
    ],
    "gov": [
        ("SEC", "https://www.sec.gov/news/pressreleases.rss"),
        ("CoinDesk Policy", "https://www.coindesk.com/policy/arc/outboundfeeds/rss/"),
        ("The Block", "https://www.theblock.co/rss.xml"),
    ],
}

_GOV_KEYWORDS = ("sec", "cftc", "regulat", "senate", "congress", "white house",
                 "treasury", "federal reserve", "fed ", "law", "bill", "policy",
                 "court", "lawsuit", "government", "irs", "clarity act", "etf")


def _parse_feed(source: str, url: str, limit: int = 12) -> list[dict]:
    try:
        raw = _http_get(url, timeout=10)
    except Exception:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    items = root.iter("item")
    out = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for it in items:
        def g(tag):
            el = it.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        title = g("title")
        link = g("link")
        pub = g("pubDate") or g("published")
        if not title:
            continue
        out.append({"title": title, "url": link, "published": pub, "source": source})
        if len(out) >= limit:
            break
    return out


@app.get("/api/crypto/news")
def crypto_news(category: str = "crypto", limit: int = 12):
    """Latest headlines. category: crypto|gov|all."""
    cats = ["crypto", "gov"] if category == "all" else [category if category in _NEWS_FEEDS else "crypto"]

    def fetch():
        items: list[dict] = []
        for c in cats:
            for source, url in _NEWS_FEEDS[c]:
                items += _parse_feed(source, url, limit=8)
        seen, dedup = set(), []
        for it in items:
            k = it["title"].lower()[:80]
            if k in seen:
                continue
            seen.add(k)
            dedup.append(it)
        # for gov filter, prefer headlines that actually mention regulation
        if category == "gov":
            gov = [i for i in dedup if any(w in i["title"].lower() for w in _GOV_KEYWORDS)]
            dedup = gov or dedup
        return dedup[:limit]
    return {"category": category, "items": _cached(f"news:{category}:{limit}", 600, fetch),
            "fetched_at": int(time.time())}


# ---------- crypto portfolio (manual holdings, stored in DB) ----------

@app.get("/api/crypto/holdings")
def get_holdings():
    with db() as con:
        rows = con.execute(
            "SELECT id, coin_id, symbol, name, amount, updated_at FROM crypto_holdings ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


class HoldingIn(BaseModel):
    coin_id: str
    symbol: str | None = None
    name: str | None = None
    amount: float


@app.post("/api/crypto/holdings")
def add_holding(body: HoldingIn):
    if body.amount is None or body.amount < 0:
        raise HTTPException(400, "amount must be >= 0")
    with db() as con:
        existing = con.execute("SELECT id FROM crypto_holdings WHERE coin_id=?", (body.coin_id,)).fetchone()
        if existing:
            con.execute("UPDATE crypto_holdings SET amount=?, symbol=COALESCE(?,symbol), "
                        "name=COALESCE(?,name), updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (body.amount, body.symbol, body.name, existing["id"]))
            rid = existing["id"]
        else:
            cur = con.execute("INSERT INTO crypto_holdings(coin_id,symbol,name,amount) VALUES(?,?,?,?)",
                              (body.coin_id, body.symbol, body.name, body.amount))
            rid = cur.lastrowid
    return {"ok": True, "id": rid}


@app.delete("/api/crypto/holdings/{hold_id}")
def delete_holding(hold_id: int):
    with db() as con:
        con.execute("DELETE FROM crypto_holdings WHERE id=?", (hold_id,))
    return {"ok": True}


class HoldingPatch(BaseModel):
    amount: float | None = None
    symbol: str | None = None
    name: str | None = None


@app.patch("/api/crypto/holdings/{hold_id}")
def update_holding(hold_id: int, body: HoldingPatch):
    if body.amount is not None and body.amount < 0:
        raise HTTPException(400, "amount must be >= 0")
    with db() as con:
        existing = con.execute("SELECT id FROM crypto_holdings WHERE id=?", (hold_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "holding not found")
        con.execute(
            "UPDATE crypto_holdings SET amount=COALESCE(?,amount), symbol=COALESCE(?,symbol), "
            "name=COALESCE(?,name), updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (body.amount, body.symbol, body.name, hold_id),
        )
    return {"ok": True, "id": hold_id}


# ---------- favorites (starred coins) ----------

@app.get("/api/crypto/favorites")
def get_favorites():
    with db() as con:
        rows = con.execute("SELECT coin_id FROM crypto_favorites ORDER BY coin_id").fetchall()
    return [r["coin_id"] for r in rows]


class FavIn(BaseModel):
    coin_id: str


@app.post("/api/crypto/favorites")
def add_favorite(body: FavIn):
    with db() as con:
        con.execute("INSERT OR IGNORE INTO crypto_favorites(coin_id) VALUES(?)", (body.coin_id,))
    return {"ok": True}


@app.delete("/api/crypto/favorites/{coin_id}")
def del_favorite(coin_id: str):
    with db() as con:
        con.execute("DELETE FROM crypto_favorites WHERE coin_id=?", (coin_id,))
    return {"ok": True}


# ---------- static ----------

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/favicon.svg")
def favicon_svg():
    return FileResponse(os.path.join(STATIC, "favicon.svg"), media_type="image/svg+xml")


@app.get("/favicon.ico")
def favicon_ico():
    # Browsers auto-request /favicon.ico; serve the SVG so the tab has an icon.
    return FileResponse(os.path.join(STATIC, "favicon.svg"), media_type="image/svg+xml")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
