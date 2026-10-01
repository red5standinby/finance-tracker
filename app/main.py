"""FastAPI app: import, dashboard, transactions, categories, rules."""
from __future__ import annotations

import io
import json
import os
import re
import difflib
import uuid
from collections import defaultdict
from datetime import date, timedelta

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import categorize as cat
from .adapters import AdapterError, detect_adapter, parse_any
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

@app.post("/api/import")
async def import_csv(file: UploadFile = File(...), account: str = Form(...),
                     bank: str = Form(""), kind: str = Form("checking"),
                     exclude: bool = Form(False)):
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    try:
        txns, detected = parse_any(raw, account)
    except AdapterError as e:
        raise HTTPException(400, str(e))
    if not txns:
        raise HTTPException(400, "No transactions parsed from file")

    batch = uuid.uuid4().hex[:12]
    inserted = skipped = 0
    with db() as con:
        acct_id = ensure_account(con, account, bank or detected, kind, exclude)
        lm = learned_map(con)
        cats = cat_by_key(con)
        for t in txns:
            cleaned = cat.clean_merchant(t.description)
            fid = make_fitid(account, t.date, t.amount, cleaned or t.description)
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
            except Exception:
                skipped += 1
        con.execute("INSERT INTO imports(id,filename,account,bank,inserted,skipped) VALUES(?,?,?,?,?,?)",
                    (batch, file.filename, account, bank or detected, inserted, skipped))

    return {"ok": True, "batch": batch, "bank": detected, "account": account,
            "inserted": inserted, "skipped_duplicates": skipped, "parsed": len(txns)}


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
                    continue
                desc = t.get("description", "")
                amt = float(t.get("amount", 0))
                raw_type = t.get("raw_type", "")
                cleaned = cat.clean_merchant(desc)
                fid = make_fitid(acct.name, d_obj, amt, cleaned or desc)
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
                except Exception:
                    skipped += 1
            con.execute("INSERT INTO imports(id,filename,account,bank,inserted,skipped) VALUES(?,?,?,?,?,?)",
                        (batch, f"{body.filename}#{acct.name}", acct.name, body.bank, inserted, skipped))
        results.append({"account": acct.name, "inserted": inserted,
                        "skipped_duplicates": skipped, "parsed": len(acct.txns)})
    total_in = sum(r["inserted"] for r in results)
    return {"ok": True, "total_inserted": total_in,
            "total_skipped": sum(r["skipped_duplicates"] for r in results), "results": results}


@app.post("/api/import/preview")
async def import_preview(file: UploadFile = File(...)):
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
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
    total_spent = round(sum(r["spent"] for r in rows if r["budget"] > 0), 2)
    return {
        "month": month,
        "rows": rows,
        "totals": {"budget": total_budget, "spent": total_spent},
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
            f"SELECT a.id, a.name, a.bank, a.exclude FROM accounts a {where} ORDER BY a.name",
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
            t_in += deposits; t_out += withdrawals; t_int += interest
            out.append({
                "id": a["id"], "name": a["name"], "bank": a["bank"],
                "deposits": round(deposits, 2),
                "withdrawals": round(withdrawals, 2),
                "interest": round(interest, 2),
                "net": round(net, 2),
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
def patch_account(acct_id: int, exclude: bool = None, name: str = None):
    with db() as con:
        if exclude is not None:
            con.execute("UPDATE accounts SET exclude=? WHERE id=?", (1 if exclude else 0, acct_id))
        if name:
            con.execute("UPDATE accounts SET name=? WHERE id=?", (name, acct_id))
    return {"ok": True}


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


# ---------- static ----------

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")
