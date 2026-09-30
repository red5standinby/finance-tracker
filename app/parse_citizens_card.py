"""Citizens Credit Card text-dump parser.

Format (statement copy/paste):
    Merchant Name
    $12.34
    Debit|Credit
    ...
    September 26, 2026        <- date header; labels the block BELOW it
    Merchant
    $1,234.56
    Debit

Sign convention (canonical): + = money in, - = money out.
  Debit  -> money OUT -> negative
  Credit -> money IN  -> positive
Amounts already carry an explicit '-' on credits in this dump; we normalize by
the Debit/Credit word so both cases agree.
"""
from __future__ import annotations

import re

MONTHS = {m: i + 1 for i, m in enumerate(
    "January February March April May June July August September "
    "October November December".split())}

_DATE_RE = re.compile(r"^([A-Z][a-z]+) (\d{1,2}), (\d{4})$")
_AMT_RE = re.compile(r"^-?\$?[\d,]+\.\d{2}$")


def _parse_date_line(s: str):
    m = _DATE_RE.match(s.strip())
    if not m:
        return None
    mon = MONTHS.get(m.group(1))
    if not mon:
        return None
    return (int(m.group(3)), mon, int(m.group(2)))


def _money(s: str):
    s = s.strip().replace("$", "").replace(",", "")
    neg = s.startswith("-")
    s = s.lstrip("-")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse_citizens_card_text(text: str):
    """Return list of dicts {date:(y,m,d), description, amount} with canonical sign."""
    lines = [l.rstrip("\r\n") for l in text.splitlines()]
    # drop blank lines but keep order
    lines = [l for l in lines if l.strip() != ""]

    txns = []
    cur_date = None
    # If transactions appear ABOVE the first date header (paste truncated at top),
    # attribute them to the first header date rather than dropping them.
    first_date = None
    i = 0
    n = len(lines)
    for j in range(n):
        if _parse_date_line(lines[j]) is not None:
            first_date = _parse_date_line(lines[j])
            break
    # A transaction is (merchant, amount, type) triple.
    while i < n:
        d = _parse_date_line(lines[i])
        if d is not None:
            cur_date = d
            i += 1
            continue
        # try merchant/amount/type triple
        if i + 2 < n and _AMT_RE.match(lines[i + 1].strip()):
            merchant = lines[i].strip()
            amt = _money(lines[i + 1])
            typ = lines[i + 2].strip().lower()
            if amt is not None and typ in ("debit", "credit"):
                eff_date = cur_date or first_date
                if eff_date is None:
                    i += 3
                    continue
                signed = -abs(amt) if typ == "debit" else abs(amt)
                txns.append({
                    "date": eff_date,
                    "description": merchant,
                    "amount": round(signed, 2),
                    "raw_type": typ,
                })
                i += 3
                continue
        # unrecognized line -> skip it
        i += 1
    return txns
