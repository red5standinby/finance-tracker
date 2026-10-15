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


def reconcile_zelle_transfers(con, lookback_days: int = 3) -> int:
    """Reclassify inbound Zelle 'payment received' credits that are actually
    internal self-moves as transfers.

    A credit is an internal move when a matching-magnitude OUTFLOW leg (a Zelle
    'payment sent' or a Jaime-Luna 'ZELLE CREDIT') exists on another row within
    `lookback_days` of the credit's date. Genuine income (e.g. the renter) has no
    such offsetting leg and is left as income.

    Only rows still tagged as income/income-source are touched, so manual
    overrides and prior reconciliations are never clobbered. Returns the number
    of rows reclassified. Idempotent.
    """
    rows = con.execute(
        """SELECT t.id, t.date, t.amount, t.description
           FROM transactions t
           WHERE t.amount > 0 AND t.category_source = 'income'
           ORDER BY t.date""").fetchall()
    out_rows = con.execute(
        """SELECT t.id, t.date, t.amount, t.description
           FROM transactions t
           WHERE t.amount < 0 ORDER BY t.date""").fetchall()
    outs = [r for r in out_rows if cat.is_zelle_out(r["description"])]
    moved = 0
    for r in rows:
        if not cat.is_zelle_in(r["description"]):
            continue
        try:
            d0 = date.fromisoformat(r["date"])
        except (ValueError, TypeError):
            continue
        amt = r["amount"]
        for o in outs:
            if abs(abs(o["amount"]) - amt) > 0.01:
                continue
            try:
                d1 = date.fromisoformat(o["date"])
            except (ValueError, TypeError):
                continue
            if abs((d1 - d0).days) <= lookback_days:
                con.execute(
                    "UPDATE transactions SET category_id=?, category_source='manual' WHERE id=?",
                    (cat_by_key(con)["transfer"]["id"], r["id"]))
                moved += 1
                break
    return moved


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
        reconcile_zelle_transfers(con)

    # Count identity groups that appear >1x in the source (the "looks like a dupe"
    # case). If any of those got skipped, it's worth flagging loudly.
    multi_identity = {k: v for k, v in seen.items() if v > 1}
    flagged = [r for r in skipped_rows if r.get("same_batch_identity")]
    warning = None
    if flagged:
        warning = (f"{len(flagged)} row(s) were skipped even though identical rows exist in "
                   f"this same file - check whether they are genuine repeats vs. true duplicates.")
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
    with db() as con:
        zelle_moved = reconcile_zelle_transfers(con)
    total_in = sum(r["inserted"] for r in results)
    return {"ok": True, "total_inserted": total_in,
            "total_skipped": sum(r["skipped_duplicates"] for r in results),
            "zelle_transfers_reconciled": zelle_moved, "results": results}


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
    savings = 0.0
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
        elif r["ckey"] in SAVINGS_KEYS:
            # savings/investing moves are not consumption: exclude from expense
            # and track separately so the grade rewards putting money aside
            savings += -amt
            by_account[r["account"]]["expense"] += -amt
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
    grade = compute_grade(income, expense, savings)
    with db() as con:
        all_income, all_expense, all_savings = _totals_for(con)
        grade["all_time"] = compute_grade(all_income, all_expense, all_savings)
        # trend: compare this window to the immediately preceding equal-length window
        trend = None
        if start and end:
            try:
                ds = date.fromisoformat(start); de = date.fromisoformat(end)
                span = (de - ds).days + 1
                prev_end = (ds - timedelta(days=1))
                prev_start = prev_end - timedelta(days=span - 1)
                pi, pe, ps = _totals_for(con, prev_start.isoformat(), prev_end.isoformat())
                pg = compute_grade(pi, pe, ps)
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
                   "savings": round(savings, 2),
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
           accounts: str | None = None, months: int = 12,
           categories: str | None = None):
    """Per-category monthly series + 3-month averages, expense-focused.

    `months` limits how many of the most recent months are returned.
    Transfers and hidden categories are always excluded (not real spend).
    `categories` (comma-separated keys) limits the expense breakdown to those
    categories; income totals are unaffected.
    """
    where, params = ["c.kind != 'transfer'", "c.hidden = 0"], []
    ids = [int(x) for x in accounts.split(",") if x.strip().isdigit()] if accounts else []
    if ids:
        # explicit selection: show it even if the account is otherwise excluded
        where.append(f"t.account_id IN ({','.join('?' * len(ids))})"); params.extend(ids)
    else:
        where.insert(0, "ac.exclude = 0")
    cat_keys = [x.strip() for x in categories.split(",") if x.strip()] if categories else []
    if cat_keys:
        # scope to expenses only, so income totals stay intact
        where.append(f"(t.amount >= 0 OR c.key IN ({','.join('?' * len(cat_keys))}))")
        params.extend(cat_keys)
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

    # 3-month averages of overall income/expense (trailing)
    recent3 = all_months[-3:]
    avg_inc = round(sum(month_income.get(m, 0.0) for m in recent3) / max(len(recent3), 1), 2)
    avg_exp = round(sum(month_expense.get(m, 0.0) for m in recent3) / max(len(recent3), 1), 2)

    # complete-month averages: only months with real income activity.
    # Partial-history months (no/negligible income recorded, e.g. credit-card
    # or savings-only imports) would otherwise count as $0 income and drag the
    # average down. A month is "complete" when its income is at least 25% of
    # the median income across income-bearing months (adaptive threshold).
    inc_months = [m for m in all_months if month_income.get(m, 0.0) > 0]
    if inc_months:
        vals = sorted(month_income[m] for m in inc_months)
        mid = len(vals) // 2
        median_inc = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
        thr = median_inc * 0.25
        complete = [m for m in inc_months if month_income.get(m, 0.0) >= thr]
    else:
        complete = []
    if not complete:
        complete = all_months
    n_c = max(len(complete), 1)
    avg_inc_all = round(sum(month_income.get(m, 0.0) for m in complete) / n_c, 2)
    avg_exp_all = round(sum(month_expense.get(m, 0.0) for m in complete) / n_c, 2)

    return {
        "months": all_months,
        "categories": cats,
        "month_series": month_series,
        "avg_3mo": {"income": avg_inc, "expense": avg_exp,
                     "net": round(avg_inc - avg_exp, 2)},
        "avg_all": {"income": avg_inc_all, "expense": avg_exp_all,
                     "net": round(avg_inc_all - avg_exp_all, 2),
                     "months": len(complete)},
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
        })    # budgeted first (by pct spent desc), then the rest by spend desc
    rows.sort(key=lambda r: (r["budget"] <= 0, -(r["pct"] or 0), -r["spent"]))

    total_budget = round(sum(r["budget"] for r in rows), 2)
    # Spend in *budgeted* categories only (drives the budget bars).
    total_spent = round(sum(r["spent"] for r in rows if r["budget"] > 0), 2)
    # ALL expense spend this month (budgeted or not) - the honest "usage" figure.
    total_spent_all = round(sum(r["spent"] for r in rows), 2)
    return {
        "month": month,
        "rows": rows,
        "categories": sorted(
            [{"key": m["key"], "name": m["name"], "color": m["color"]} for m in meta.values()],
            key=lambda x: x["name"].lower()),
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

    Matches txns whose cleaned merchant *contains* the bill's match key
    (case-insensitive) - merchants are often fragmented across variants
    (NETFLIX 1 / NETFLIX COM ...), so substring matching is intentional.

    Installments: when payments_per_month > 1 (e.g. weekly daycare), the
    bill is 'paid' once that many matching charges are found this month,
    'partial' while fewer have posted (paid_amount sums what came in),
    and due/overdue/upcoming otherwise.
    """
    key = (bill["match_cleaned"] or "").lower()
    try:
        per_month = int(bill["payments_per_month"] or 1)
    except (KeyError, IndexError, TypeError):
        per_month = 1
    if per_month < 1:
        per_month = 1
    today = date.today()
    # Future-dated bills: starts_on is the first due/start date ('YYYY-MM-DD').
    # Until that date arrives, the bill is 'scheduled' (upcoming, not yet owed).
    # Once today reaches starts_on, normal due/overdue logic takes over.
    starts_on = None
    try:
        starts_on = bill["starts_on"]
    except (KeyError, IndexError):
        starts_on = None
    if starts_on:
        try:
            start_d = date.fromisoformat(str(starts_on)[:10])
        except ValueError:
            start_d = None
        if start_d and today < start_d and month <= start_d.strftime("%Y-%m"):
            return {
                "status": "scheduled", "paid_date": None, "paid_amount": None,
                "variance": None, "manual": False, "starts_on": starts_on,
                "paid_count": 0, "expected_count": per_month, "remaining_count": per_month,
                "per_payment": round((bill["expected_amount"] or 0) / per_month, 2),
            }
    # Installment bills (weekly / multi-payment) are matched on a window
    # starting at the calendar month and spanning ~per_month weekly cycles
    # (plus a few days of tail so a late cycle still lands). No back-grace:
    # charges are attributed forward, so a boundary charge can't be counted
    # in two months at once.
    if per_month > 1:
        m0 = date(int(month[:4]), int(month[5:7]), 1)
        span = per_month * 7 + 3
        if month == today.strftime("%Y-%m"):
            win_end = min(today, m0 + timedelta(days=span))
        else:
            if int(month[5:7]) == 12:
                m_end = date(int(month[:4]), 12, 31)
            else:
                m_end = date(int(month[:4]), int(month[5:7]) + 1, 1) - timedelta(days=1)
            win_end = min(m_end, m0 + timedelta(days=span))
        rows = con.execute("""
            SELECT t.date, t.amount FROM transactions t
            WHERE lower(t.cleaned) LIKE ? AND t.amount < 0
              AND t.date >= ? AND t.date <= ?
            ORDER BY t.date DESC""", (f"%{key}%", m0.isoformat(), win_end.isoformat())).fetchall()
    else:
        rows = con.execute("""
            SELECT t.date, t.amount FROM transactions t
            WHERE lower(t.cleaned) LIKE ? AND substr(t.date,1,7) = ? AND t.amount < 0
            ORDER BY t.date DESC""", (f"%{key}%", month)).fetchall()
    status, paid_date, paid_amount = "due", None, None
    manual = None
    try:
        manual = bill["manual_paid_month"]
    except (KeyError, IndexError):
        manual = None
    if manual == month:
        # user hand-marked this month paid (cash / other account / early)
        return {"status": "paid", "paid_date": None, "paid_amount": bill["expected_amount"],
                "variance": None, "manual": True,
                "paid_count": per_month, "expected_count": per_month,
                "remaining_count": 0, "per_payment": round((bill["expected_amount"] or 0)/per_month, 2)}
    found = len(rows)
    paid_sum = round(sum(-r["amount"] for r in rows), 2)
    per_payment = round((bill["expected_amount"] or 0) / per_month, 2)
    # Installment bills: due/overdue windows are based on the cycle anchoring,
    # but the simplest robust signal is "have we seen per_month charges yet".
    if found >= per_month:
        # fully paid: count the most-recent `per_month` charges in the window
        status = "paid"
        paid_date = rows[0]["date"]
        paid_amount = round(sum(-r["amount"] for r in rows[:per_month]), 2)
    elif found > 0 and per_month > 1:
        # some installments in - still accruing
        status = "partial"
        paid_date = rows[0]["date"]
        paid_amount = paid_sum
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
        "variance": round(paid_amount - bill["expected_amount"], 2) if (paid_amount is not None and status == "paid") else None,
        "manual": False,
        "paid_count": min(found, per_month) if status in ("paid", "partial") else 0,
        "expected_count": per_month,
        "remaining_count": max(0, per_month - found),
        "per_payment": per_payment,
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
    payments_per_month: int | None = None
    starts_on: str | None = None   # 'YYYY-MM-DD' first due / start date; null = already active


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
        "partial": sum(1 for r in out if r["status"] == "partial"),
        "due": sum(1 for r in out if r["status"] in ("due", "upcoming", "partial")),
        "overdue": sum(1 for r in out if r["status"] in ("overdue", "missed")),
        "scheduled": sum(1 for r in out if r["status"] == "scheduled"),
        "total": len(out),
        "payable": sum(1 for r in out if r["status"] != "scheduled"),
        "expected": round(sum(r.get("expected_amount") or 0 for r in out), 2),
        "paid_amount": round(sum((r.get("paid_amount") or 0) for r in out if r["status"] in ("paid", "partial")), 2),
        "left": round(sum(
            (r.get("expected_amount") or 0) - (r.get("paid_amount") or 0) if r["status"] == "partial"
            else (0 if r["status"] in ("paid", "scheduled") else (r.get("expected_amount") or 0))
            for r in out), 2),
    }
    # surface overdue first, then due/partial, then paid
    order = {"overdue": 0, "missed": 1, "due": 2, "upcoming": 3, "partial": 3, "scheduled": 4, "paid": 5}
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


@app.get("/api/merchants")
def list_merchants(q: str | None = None, limit: int = 200):
    """Distinct cleaned merchant names seen in transactions, most frequent first.
    Powers the autocomplete datalist on the bill 'match merchant' field."""
    with db() as con:
        rows = con.execute("""
            SELECT COALESCE(NULLIF(TRIM(t.cleaned),''), TRIM(t.description)) AS name,
                   COUNT(*) n
            FROM transactions t
            WHERE COALESCE(NULLIF(TRIM(t.cleaned),''), TRIM(t.description)) IS NOT NULL
            GROUP BY name
            ORDER BY n DESC, name ASC
            LIMIT ?
        """, (max(1, min(2000, limit)),)).fetchall()
    names = [r["name"] for r in rows if r["name"]]
    if q:
        ql = q.lower()
        names = [n for n in names if ql in n.lower()]
    return {"merchants": names}


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
                "partial": sum(1 for r in items if r["status"] == "partial"),
                "due": sum(1 for r in items if r["status"] in ("due", "upcoming", "partial")),
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
        con.execute("""INSERT INTO bills(name,match_cleaned,expected_amount,due_day,kind,category_key,active,payments_per_month,starts_on)
            VALUES(?,?,?,?,?,?,1,?,?)
            ON CONFLICT(match_cleaned) DO UPDATE SET
              name=excluded.name, expected_amount=excluded.expected_amount,
              due_day=excluded.due_day, kind=excluded.kind,
              category_key=excluded.category_key, active=1,
              payments_per_month=excluded.payments_per_month,
              starts_on=excluded.starts_on""",
            (body.name or key.title(), key, body.expected_amount or 0,
             body.due_day or 0, body.kind or "bill", body.category_key or "",
             body.payments_per_month or 1, body.starts_on or None))
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
        if body.payments_per_month is not None: sets.append("payments_per_month=?"); params.append(max(1, body.payments_per_month))
        if body.starts_on is not None:       sets.append("starts_on=?");       params.append(body.starts_on or None)
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
        agg = con.execute(f"""SELECT COALESCE(SUM(t.amount),0) s
            FROM transactions t
            JOIN categories c ON c.id=t.category_id
            JOIN accounts ac ON ac.id=t.account_id {w}""", params).fetchone()["s"]
        rows = con.execute(f"""
            SELECT t.id, t.date, t.description, t.cleaned, t.amount, t.raw_type,
                   t.category_source, t.memo, c.key ckey, c.name cname, c.color ccolor,
                   ac.name account
            FROM transactions t
            JOIN categories c ON c.id=t.category_id
            JOIN accounts ac ON ac.id=t.account_id
            {w} ORDER BY {col} {direction}, t.id DESC LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()
    return {"total": total, "sum": round(agg, 2), "items": [dict(r) for r in rows]}


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


@app.get("/api/savings/history")
def savings_history(months: int = 12, accounts: str | None = None):
    """Month-end savings balance walk, for the Home savings-vs-crypto chart.

    Savings moves are transfers (not income/expense), so we reconstruct the
    balance from the stored anchor + activity after the anchor date, mirroring
    the /api/savings maths, and emit one point per month-end (plus today).
    """
    ids = [int(x) for x in accounts.split(",") if x.strip().isdigit()] if accounts else []
    where = "WHERE a.kind='savings'"
    params: list = []
    if ids:
        where = "WHERE a.kind='savings' AND a.id IN (%s)" % ",".join("?" * len(ids))
        params += ids
    with db() as con:
        accts = con.execute(f"SELECT * FROM accounts a {where}", params).fetchall()
        all_txns: list = []
        anchor_balance = 0.0
        anchor_date = ""
        has_anchor = False
        for a in accts:
            txns = con.execute(
                "SELECT date, amount FROM transactions WHERE account_id=? ORDER BY date",
                (a["id"],),
            ).fetchall()
            all_txns += [dict(t) for t in txns]
            if a["balance"] is not None:
                has_anchor = True
                anchor_balance += a["balance"]
                # use the account's own anchor date; empty -> counts everything after ""
                ad = a["balance_date"] or ""
                if ad > anchor_date:
                    anchor_date = ad

    if not has_anchor:
        return {"months": [], "current": None, "current_month": None, "txn_net": 0.0}

    all_txns.sort(key=lambda t: t["date"] or "")
    txn_total = round(sum(t["amount"] for t in all_txns), 2)
    # activity after the latest anchor date (each account contributes its own)
    post = round(sum(t["amount"] for t in all_txns if (t["date"] or "") > anchor_date), 2)
    current = round(anchor_balance + post, 2)

    # build month-end series by walking the anchor forward with dated activity
    today = date.today()
    months_out: list = []
    ends = []
    for i in range(months - 1, -1, -1):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        last_day = (date(ny, nm, 1) - timedelta(days=1)).isoformat()
        ends.append((f"{y:04d}-{m:02d}", last_day))

    # per-account backward walk from the live balance:
    #   balance(eom) = current - sum(activity after eom)
    # This reconstructs history correctly even when the account only has a
    # single recent snapshot (e.g. War Chest anchored 2026-09-30), which the
    # old forward walk could not do (it produced a flat line).
    for ym, eom in ends:
        bal = 0.0
        ok = False
        with db() as con:
            for a in accts:
                if a["balance"] is None:
                    continue
                ok = True
                ad = a["balance_date"] or ""
                anchor = a["balance"] + con.execute(
                    "SELECT COALESCE(SUM(amount),0) s FROM transactions "
                    "WHERE account_id=? AND date > ?",
                    (a["id"], ad),
                ).fetchone()["s"]
                after = con.execute(
                    "SELECT COALESCE(SUM(amount),0) s FROM transactions "
                    "WHERE account_id=? AND date > ?",
                    (a["id"], eom),
                ).fetchone()["s"]
                bal += anchor - (after or 0)
        months_out.append({"month": ym, "balance": round(bal, 2) if ok else None})

    # make sure the most recent point reflects today's live balance
    if months_out:
        months_out[-1]["balance"] = current

    return {
        "months": months_out,
        "current": current,
        "current_month": today.strftime("%Y-%m"),
        "txn_net": txn_total,
        "anchor_date": anchor_date,
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


# ---------------- Credit-card paydown tool ----------------
class PaydownBody(BaseModel):
    balance: float | None = None
    apr: float | None = None
    min_payment: float | None = None
    balance_date: str | None = None


@app.get("/api/paydown")
def get_paydown():
    """Credit cards + their paydown config, combined totals, and payment stats."""
    from datetime import date as _date
    with db() as con:
        accts = [dict(r) for r in con.execute(
            "SELECT id, name, bank, balance, balance_date, apr, min_payment "
            "FROM accounts WHERE kind='credit' AND COALESCE(exclude,0)=0 ORDER BY name")]
        cards = []
        for a in accts:
            aid = a["id"]
            # payments = positive-amount rows (credits/payments toward the card)
            row = con.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(amount),0) total, MAX(date) last "
                "FROM transactions WHERE account_id=? AND amount > 0", (aid,)).fetchone()
            spend = con.execute(
                "SELECT COALESCE(SUM(amount),0) s FROM transactions "
                "WHERE account_id=? AND amount < 0", (aid,)).fetchone()["s"]
            cards.append({
                "id": aid,
                "name": a["name"],
                "bank": a["bank"],
                "balance": a["balance"],
                "balance_date": a["balance_date"],
                "apr": a["apr"],
                "min_payment": a["min_payment"],
                "payments_count": row["n"],
                "payments_total": round(row["total"], 2),
                "payments_last": row["last"],
                "charges_total": round(abs(spend), 2),
            })
    # Only cards with an actual outstanding balance count toward owed/min/count.
    # A paid-off card (balance 0.0) still carries a stored min_payment and must
    # NOT be summed into the household minimum or the "N of M cards" figure.
    known = [c for c in cards if c["balance"] is not None and c["balance"] > 0]
    total_owed = round(sum(c["balance"] for c in known), 2)
    total_min = round(sum((c["min_payment"] or 0) for c in known), 2)
    # balance-weighted average APR
    denom = sum(c["balance"] for c in known) or 0
    avg_apr = round(sum((c["apr"] or 0) * c["balance"] for c in known) / denom, 2) if denom else 0
    return {
        "cards": cards,
        "totals": {
            "owed": total_owed,
            "min": total_min,
            "avg_apr": avg_apr,
            "cards_with_balance": len(known),
            "cards_total": len(cards),
        },
    }


@app.patch("/api/paydown/{acct_id}")
def patch_paydown(acct_id: int, body: PaydownBody):
    from datetime import date as _date
    sets, vals = [], []
    for col in ("balance", "apr", "min_payment"):
        v = getattr(body, col)
        if v is not None:
            sets.append(f"{col}=?")
            vals.append(v)
    if body.balance is not None:
        sets.append("balance_date=?")
        vals.append(body.balance_date or _date.today().isoformat())
    if not sets:
        raise HTTPException(400, "nothing to update")
    vals.append(acct_id)
    with db() as con:
        cur = con.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=? AND kind='credit'", vals)
        if cur.rowcount == 0:
            raise HTTPException(404, "credit account not found")
    return {"ok": True}


@app.get("/api/paydown/{acct_id}/payments")
def get_paydown_payments(acct_id: int, limit: int = 100):
    with db() as con:
        rows = con.execute(
            "SELECT id, date, description, cleaned, amount, account_id "
            "FROM transactions WHERE account_id=? AND amount > 0 "
            "ORDER BY date DESC, id DESC LIMIT ?", (acct_id, limit)).fetchall()
    return [dict(r) for r in rows]


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


# ---------- AI / API costs ----------
# Usage-cost CSVs (e.g. DeepSeek per-day spend). Kept separate from bank
# transactions so operating cost is tracked without distorting budget totals.
# CSV columns: user_id, start_time_iso, end_time_iso, model, wallet_type, cost, currency

@app.post("/api/ai-costs/import")
async def ai_costs_import(file: UploadFile = File(...)):
    import csv as _csv
    from io import StringIO
    from collections import defaultdict
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = _csv.DictReader(StringIO(raw))
    if not reader.fieldnames:
        raise HTTPException(400, "Empty CSV")

    def norm(k):
        return (k or "").strip().lower()
    fset = {norm(k) for k in reader.fieldnames}
    # DeepSeek usage export: metric lines with a per-token price and a
    # token/request count in `amount`. Cost = price * amount, summed per day.
    is_usage_format = ({"type", "price", "amount"} <= fset) and ("cost" not in fset)

    rows, inserted, skipped = [], 0, 0

    if is_usage_format:
        agg = defaultdict(lambda: {"cost": 0.0, "uid": "", "model": "",
                                   "wallet": "", "end": None, "cur": "USD",
                                   "requests": 0})
        for r in reader:
            rr = {norm(k): (v or "").strip() for k, v in r.items()}
            typ = (rr.get("type") or "").lower()
            start = rr.get("start_time_iso") or rr.get("start_time") or ""
            d = start[:10]
            if not d:
                skipped += 1
                continue
            a = agg[d]
            a["uid"] = a["uid"] or rr.get("user_id", "")
            a["model"] = a["model"] or rr.get("model", "")
            a["wallet"] = a["wallet"] or rr.get("api_key_name", "")
            a["end"] = a["end"] or (rr.get("end_time_iso") or rr.get("end_time") or None)
            if typ == "request_count":
                try:
                    a["requests"] += int(float(rr.get("amount") or 0))
                except ValueError:
                    pass
                continue  # counts only, no cost
            try:
                price = float(rr.get("price") or 0)
                amt = float(rr.get("amount") or 0)
            except ValueError:
                skipped += 1
                continue
            a["cost"] += price * amt
        for d, a in agg.items():
            rows.append((a["uid"], d, a["end"], a["model"], a["wallet"],
                         round(a["cost"], 8), a["requests"], a["cur"]))
    else:
        for r in reader:
            def g(*names):
                for n in names:
                    for k in r.keys():
                        if k and k.strip().lower() == n:
                            return (r[k] or "").strip()
                return ""
            uid = g("user_id")
            start = g("start_time_iso", "start_time")
            end = g("end_time_iso", "end_time")
            model = g("model")
            wallet = g("wallet_type")
            cost_s = g("cost", "amount")
            req_s = g("request_count", "requests")
            cur = g("currency") or "USD"
            if not start and not model and not cost_s:
                continue
            try:
                cost = float(cost_s or 0)
            except ValueError:
                skipped += 1
                continue
            try:
                reqs = int(float(req_s)) if req_s else 0
            except ValueError:
                reqs = 0
            d = start[:10] if start else ""
            ed = end[:10] if end else None
            if not d:
                skipped += 1
                continue
            rows.append((uid, d, ed, model, wallet, round(cost, 8), reqs, cur))
    with db() as con:
        for row in rows:
            cur = con.execute("""INSERT OR IGNORE INTO ai_costs
                (provider_user,date,end_date,model,wallet_type,cost,requests,currency)
                VALUES(?,?,?,?,?,?,?,?)""", row)
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    return {"inserted": inserted, "skipped": skipped, "total": len(rows)}


@app.get("/api/ai-costs")
def ai_costs(start: str | None = None, end: str | None = None,
             model: str | None = None, months: int = 12):
    """Daily AI cost series + rollups (total, by day, by month, by model).

    `months` limits the monthly rollup window (most recent N). Totals are
    all-time unless start/end given.
    """
    where, params = [], []
    if start:
        where.append("date >= ?"); params.append(start)
    if end:
        where.append("date <= ?"); params.append(end)
    if model:
        where.append("model = ?"); params.append(model)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    with db() as con:
        daily = con.execute(f"""SELECT date, ROUND(SUM(cost),6) cost, COUNT(*) n,
            COALESCE(SUM(requests),0) requests
            FROM ai_costs{wsql} GROUP BY date ORDER BY date""", params).fetchall()
        monthly = con.execute(f"""SELECT substr(date,1,7) month, ROUND(SUM(cost),6) cost, COUNT(*) n,
            COALESCE(SUM(requests),0) requests
            FROM ai_costs{wsql} GROUP BY substr(date,1,7) ORDER BY month""", params).fetchall()
        bymodel = con.execute(f"""SELECT model, ROUND(SUM(cost),6) cost, COUNT(*) n,
            COALESCE(SUM(requests),0) requests
            FROM ai_costs{wsql} GROUP BY model ORDER BY cost DESC""", params).fetchall()
        bywallet = con.execute(f"""SELECT wallet_type, ROUND(SUM(cost),6) cost, COUNT(*) n,
            COALESCE(SUM(requests),0) requests
            FROM ai_costs{wsql} GROUP BY wallet_type ORDER BY cost DESC""", params).fetchall()
        grand = con.execute(f"""SELECT ROUND(COALESCE(SUM(cost),0),6) c,
            COALESCE(SUM(requests),0) requests FROM ai_costs{wsql}""", params).fetchone()
        spans = con.execute(f"SELECT MIN(date) a, MAX(date) b, COUNT(*) n FROM ai_costs{wsql}", params).fetchone()
        # model list for the filter dropdown (unfiltered)
        models = con.execute("SELECT DISTINCT model FROM ai_costs WHERE model<>'' ORDER BY model").fetchall()
    daily = [dict(r) for r in daily]
    monthly = [dict(r) for r in monthly]
    if months and len(monthly) > months:
        monthly = monthly[-months:]
    return {
        "total": grand["c"] if grand else 0,
        "requests": grand["requests"] if grand else 0,
        "count": spans["n"] if spans else 0,
        "first_date": spans["a"] if spans else None,
        "last_date": spans["b"] if spans else None,
        "daily": daily,
        "monthly": monthly,
        "by_model": [dict(r) for r in bymodel],
        "by_wallet": [dict(r) for r in bywallet],
        "models": [r["model"] for r in models],
    }


@app.delete("/api/ai-costs")
def ai_costs_clear(confirm: str = ""):
    if confirm != "yes":
        raise HTTPException(400, "Pass confirm=yes to clear all AI cost rows")
    with db() as con:
        n = con.execute("DELETE FROM ai_costs").rowcount
    return {"deleted": n}


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


@app.post("/api/reconcile-transfers")
def reconcile_transfers_endpoint():
    """Reclassify inbound Zelle 'payment received' credits that are actually
    internal self-moves (matched by an offsetting outgoing Zelle leg) as
    transfers. Idempotent; safe to run any time."""
    with db() as con:
        moved = reconcile_zelle_transfers(con)
    return {"ok": True, "reclassified": moved}


# ---------- grading ----------

GRADE_SCALE = [
    (0.40, "A+", "#16a34a"),
    (0.30, "A",  "#22c55e"),
    (0.20, "B",  "#84cc16"),
    (0.10, "C",  "#eab308"),
    (0.00, "D",  "#f97316"),
]
GRADE_F = ("F", "#ef4444")

# Categories that represent deliberate saving/investing, not consumption.
# Money moved here is pulled out of "expense" and counted as a positive
# savings contribution instead (option B), so the grade rewards putting
# money aside rather than penalizing it.
SAVINGS_KEYS = {"bitcoin", "savings_deposit", "savings_interest", "college_savings"}


def compute_grade(income: float, expense: float, savings: float = 0.0) -> dict:
    """Grade a period by savings rate.

    Savings/investing categories (see SAVINGS_KEYS) are NOT consumption: money
    moved into the War Chest or Bitcoin is excluded from `expense` and counted
    as a positive contribution. So:

        effective_spend = expense - savings      (true consumption)
        rate = (income - effective_spend) / income

    - No income AND no expense -> None/N/A (empty period)
    - No income but there IS spending -> F (you spent with nothing coming in)
    - Negative savings rate -> F
    """
    effective_spend = expense - savings
    if income <= 0:
        if expense <= 0:
            return {"grade": "N/A", "color": "#64748b", "rate": None,
                    "net": 0.0, "income": 0.0, "expense": 0.0, "savings": round(savings, 2)}
        return {"grade": GRADE_F[0], "color": GRADE_F[1], "rate": None,
                "net": round(income - effective_spend, 2), "income": round(income, 2),
                "expense": round(effective_spend, 2), "savings": round(savings, 2)}
    rate = (income - effective_spend) / income
    for threshold, letter, color in GRADE_SCALE:
        if rate >= threshold:
            return {"grade": letter, "color": color, "rate": round(rate * 100, 1),
                    "net": round(income - effective_spend, 2), "income": round(income, 2),
                    "expense": round(effective_spend, 2), "savings": round(savings, 2)}
    return {"grade": GRADE_F[0], "color": GRADE_F[1], "rate": round(rate * 100, 1),
            "net": round(income - effective_spend, 2), "income": round(income, 2),
            "expense": round(effective_spend, 2), "savings": round(savings, 2)}


def _totals_for(con, start=None, end=None, exclude_zero_income=True) -> tuple[float, float, float]:
    """Return (income, expense, savings) for a date window, excluding
    transfers, hidden categories, and excluded accounts. `expense` EXCLUDES
    savings/investing categories (see SAVINGS_KEYS); those are returned
    separately in `savings` so callers can grade on true consumption."""
    where, params = ["ac.exclude = 0", "c.kind != 'transfer'", "c.hidden = 0"], []
    if start:
        where.append("t.date >= ?"); params.append(start)
    if end:
        where.append("t.date <= ?"); params.append(end)
    w = " AND ".join(where)
    rows = con.execute(f"""
        SELECT t.amount, c.kind, c.key FROM transactions t
        JOIN categories c ON c.id = t.category_id
        JOIN accounts ac ON ac.id = t.account_id
        WHERE {w}
    """, params).fetchall()
    income = sum(r["amount"] for r in rows if r["amount"] > 0)
    expense = 0.0
    savings = 0.0
    for r in rows:
        if r["amount"] >= 0:
            continue
        if r["key"] in SAVINGS_KEYS:
            savings += -r["amount"]
        else:
            expense += -r["amount"]
    return round(income, 2), round(expense, 2), round(savings, 2)


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


@app.get("/api/crypto/price")
def crypto_price(id: str = "bitcoin", vs: str = "usd"):
    """Single live spot price + 24h change — used by the BTC stacking calculator.

    Returns a small flat payload so the client can seed an input field without
    pulling the whole markets list. Falls back to (price=None) on API failure so
    the calculator still renders (the user can type a price manually)."""
    def fetch():
        return _cg("/simple/price", {
            "ids": id, "vs_currencies": vs,
            "include_24hr_change": "true",
        })
    try:
        data = _cached(f"price:{id}:{vs}", 60, fetch)
        row = (data or {}).get(id) or {}
        price = row.get(vs)
        chg = row.get(f"{vs}_24h_change")
        return {"id": id, "vs": vs, "price": price, "chg_24h": chg,
                "fetched_at": int(time.time())}
    except Exception as e:
        return {"id": id, "vs": vs, "price": None, "chg_24h": None,
                "error": str(e), "fetched_at": int(time.time())}


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
    # Economy: debt, bonds, the Federal Reserve, rates, macro
    "economy": [
        ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
        ("CNBC Economy", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
        ("Reuters Bonds", "https://www.reutersagency.com/feed/?best-topics=bonds&post_type=best"),
        ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ],
    "bonds": [
        ("Reuters Bonds", "https://www.reutersagency.com/feed/?best-topics=bonds&post_type=best"),
        ("MarketWatch Bonds", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
    ],
}

# Economy: keywords used to keep the feed on-topic (debt / bonds / Fed / rates)
_ECON_KEYWORDS = ("debt", "deficit", "treasury", "yield", "bond", "bonds", "fed",
                  "federal reserve", "interest rate", "rate cut", "rate hike", "inflation",
                  "cpi", "gdp", "recession", "fomc", "powell", "central bank", "stimulus",
                  "auction", "10-year", "2-year", "curve", "macro", "tariff", "monetary")

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


# Economy/bonds feeds are served by /api/economy/news, but the home news card
# lets users switch to Economy alongside Crypto and Policy.
_NEWS_HOME_CATS = ("crypto", "gov", "economy")


@app.get("/api/crypto/news")
def crypto_news(category: str = "crypto", limit: int = 12):
    """Latest headlines. category: crypto|gov|economy|all."""
    cats = list(_NEWS_HOME_CATS) if category == "all" else [category if category in _NEWS_FEEDS else "crypto"]

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


@app.get("/api/economy/news")
def economy_news(category: str = "economy", limit: int = 14):
    """Economy headlines: debt, treasuries/bonds, the Federal Reserve, rates, macro.

    category: economy|bonds|all. Falls back gracefully if a feed is unreachable.
    """
    cats = ["economy", "bonds"] if category == "all" else [category if category in _NEWS_FEEDS else "economy"]

    def fetch():
        items: list[dict] = []
        for c in cats:
            for source, url in _NEWS_FEEDS[c]:
                items += _parse_feed(source, url, limit=10)
        seen, dedup = set(), []
        for it in items:
            k = it["title"].lower()[:80]
            if k in seen:
                continue
            seen.add(k)
            dedup.append(it)
        # prefer on-topic macro headlines, but don't return empty if none match
        on = [i for i in dedup if any(w in i["title"].lower() for w in _ECON_KEYWORDS)]
        ordered = on + [i for i in dedup if i not in on]
        return ordered[:limit]
    return {"category": category, "items": _cached(f"econnews:{category}:{limit}", 600, fetch),
            "fetched_at": int(time.time())}


# Treasury yields via Yahoo Finance chart endpoint (no API key). Maps index
# symbols to friendly labels. Values are the latest close, in percent.
_ECON_SYMBOLS = [
    ("10y", "^TNX", "10-Yr Treasury"),
    ("5y", "^FVX", "5-Yr Treasury"),
    ("2y", "2YY=F", "2-Yr Treasury"),
    ("13w", "^IRX", "13-Wk T-Bill"),
    ("10y2y", "^TNX-^UST2YR", "10Y – 2Y Spread"),
]


def _yahoo_last(symbol: str) -> float | None:
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=5d&interval=1d"
        data = json.loads(_http_get(url, timeout=8))
        res = (data.get("chart") or {}).get("result") or []
        if not res:
            return None
        closes = ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        vals = [c for c in closes if c is not None]
        return float(vals[-1]) if vals else None
    except Exception:
        return None


@app.get("/api/economy/markets")
def economy_markets():
    """Reference market rates: treasury yields + derived 10Y/2Y spread."""
    def fetch():
        out = {}
        raw = {k: _yahoo_last(sym) for k, sym, _ in _ECON_SYMBOLS if "-" not in sym}
        for k, sym, label in _ECON_SYMBOLS:
            if k == "10y2y":
                a, b = raw.get("10y"), raw.get("2y")
                v = round(a - b, 3) if (a is not None and b is not None) else None
            else:
                v = raw.get(k)
            out[k] = {"label": label, "value": v}
        return out
    data = _cached("econmarkets", 900, fetch)
    return {"rates": data, "fetched_at": int(time.time())}


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


# ---------- in-app chat (piped to the OpenClaw agent) ----------

CHAT_SESSION_KEY = os.environ.get("CHOMBI_CHAT_SESSION", "agent:main:budget-app-chat")
CHAT_TIMEOUT = int(os.environ.get("CHOMBI_CHAT_TIMEOUT", "120"))


def _openclaw_cmd():
    """Return the command prefix to run the openclaw CLI.

    OpenClaw's launcher is `#!/usr/bin/env node`, and the host has an older
    system node (v20) earlier in PATH than the bundled v22 runtime it needs.
    So we run the CLI's .mjs entrypoint explicitly with the bundled node when
    we can find it, falling back to the bare `openclaw` binary otherwise.
    """
    env_bin = os.environ.get("OPENCLAW_BIN")
    tools = os.path.expanduser("~/.openclaw/tools")
    node_v22 = None
    if os.path.isdir(tools):
        for d in sorted(os.listdir(tools), reverse=True):
            cand = os.path.join(tools, d, "bin", "node")
            if os.path.exists(cand):
                node_v22 = cand
                break
    mjs = None
    if os.path.isdir(tools):
        for d in sorted(os.listdir(tools), reverse=True):
            cand = os.path.join(tools, d, "lib", "node_modules", "openclaw", "openclaw.mjs")
            if os.path.exists(cand):
                mjs = cand
                break
    if env_bin and os.path.exists(env_bin):
        return [node_v22, env_bin] if (node_v22 and env_bin.endswith(".mjs")) else [env_bin]
    if node_v22 and mjs:
        return [node_v22, mjs]
    from shutil import which
    found = which("openclaw")
    return [found] if found else ["openclaw"]


class ChatIn(BaseModel):
    message: str
    context: str | None = None   # short situational hint (tab, month, figures)


@app.post("/api/chat")
def chat(body: ChatIn):
    """Forward an in-app chat turn to the OpenClaw agent and return its reply.

    Runs the same agent codepath as `openclaw agent`, on a fixed session key so
    the conversation thread persists between app reloads. Kept synchronous and
    generous on timeout because a real agent turn (with tool use) can take a
    while; the SPA shows a 'thinking' state meanwhile.
    """
    import subprocess
    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(400, "empty message")
    # Optional one-line situational context so 'why is this month an F?' works.
    if body.context:
        ctx = body.context.strip()[:500]
        msg = f"[In-app chat from the budget app. Current view: {ctx}]\n\n{msg}" 
    cmd = _openclaw_cmd() + ["agent", "--session-key", CHAT_SESSION_KEY,
           "--json", "-m", msg]
    env = dict(os.environ)
    node_dir = os.path.dirname(_openclaw_cmd()[0])
    if node_dir and node_dir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=CHAT_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "The agent took too long to respond. Try again.")
    except FileNotFoundError:
        raise HTTPException(503, "OpenClaw CLI not found on the server host.")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise HTTPException(502, f"Agent run failed: {detail}")
    try:
        data = json.loads(proc.stdout)
        payloads = (data.get("result") or {}).get("payloads") or []
        reply = "\n".join(p.get("text", "") for p in payloads if p.get("text")).strip()
        if not reply:
            reply = "(no reply)"
    except (json.JSONDecodeError, AttributeError):
        reply = (proc.stdout or "").strip()[-4000:] or "(no reply)"
    return {"reply": reply}


@app.get("/favicon.svg")
def favicon_svg():
    return FileResponse(os.path.join(STATIC, "favicon.svg"), media_type="image/svg+xml")


@app.get("/favicon.ico")
def favicon_ico():
    # Browsers auto-request /favicon.ico; serve the SVG so the tab has an icon.
    return FileResponse(os.path.join(STATIC, "favicon.svg"), media_type="image/svg+xml")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
