"""Parse a Capital One 360 multi-account PDF statement into per-account transactions.

Emits JSON on stdout: {"period": "...", "accounts": {"<name>": [ {date,description,amount,...}, ...]}}
Sign convention: + = money in, - = money out.

Usage:  .venv/bin/python parse_capone_pdf.py <statement.pdf>
"""
from __future__ import annotations
import json
import re
import sys
from datetime import datetime

from pypdf import PdfReader

# Account header lines look like:  "War Chest - 36241812902"  or  "360 Checking - 36128242020"
ACCT_RE = re.compile(r"^(?P<name>.+?)\s+-\s+(?P<num>\d{6,})$")
# Transaction rows: "Aug 12 Deposit from VENMO CASHOUT Credit + $1,332.50 $1,337.18"
TXN_RE = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<desc>.+?)\s+"
    r"(?:(?P<kind>Credit|Debit)\s+)?(?P<sign>[+\-])\s*\$?(?P<amt>[\d,]+\.\d{2})\s+"
    r"\$?(?P<bal>[\d,]+\.\d{2})\s*$"
)
MONTHS = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}


def _money(s: str) -> float:
    return float(s.replace(",", ""))


def parse(path: str) -> dict:
    reader = PdfReader(path)
    text = "\n".join((p.extract_text() or "") for p in reader.pages)

    # statement period, e.g. "Aug 1 - Aug 31, 2026"
    pm = re.search(r"Aug\s+1\s*-\s*Aug\s+31,\s*(\d{4})", text)
    year = int(pm.group(1)) if pm else datetime.now().year

    accounts: dict[str, dict] = {}
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = ACCT_RE.match(line)
        if m:
            cur = m.group("name").strip()
            if cur not in accounts:
                accounts[cur] = {"number": m.group("num"), "txns": []}
            continue
        if cur is None:
            continue
        # skip known non-transaction noise
        if line.startswith(("DATE DESCRIPTION", "Page ", "STATEMENT PERIOD",
                            "capitalone.com", "®", "(")):
            continue
        t = TXN_RE.match(line)
        if not t:
            continue
        desc = t.group("desc").strip()
        if desc.lower() in ("opening balance", "closing balance"):
            continue
        mon = MONTHS.get(t.group("mon"))
        if not mon:
            continue
        amt = _money(t.group("amt"))
        kind = (t.group("kind") or "").lower()
        if t.group("sign") == "-" or kind == "debit":
            amt = -abs(amt)
        else:
            amt = abs(amt)
        accounts[cur]["txns"].append({
            "date": f"{year:04d}-{mon:02d}-{int(t.group('day')):02d}",
            "description": desc,
            "amount": round(amt, 2),
            "balance": _money(t.group("bal")),
            "raw_type": kind or ("credit" if amt >= 0 else "debit"),
        })

    period = f"{year}-08-01..{year}-08-31" if pm else ""
    return {"period": period, "accounts": accounts}


if __name__ == "__main__":
    print(json.dumps(parse(sys.argv[1]), indent=2))
