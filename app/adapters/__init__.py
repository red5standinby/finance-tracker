"""Bank CSV adapters. Each adapter normalizes a bank-specific CSV into NormalizedTxn.

Sign convention (canonical): amount > 0 = money IN, amount < 0 = money OUT.
Every adapter must honor this regardless of the source bank's convention.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


@dataclass
class NormalizedTxn:
    date: date
    description: str
    amount: float          # + = money in, - = money out
    account: str = ""
    bank: str = ""
    raw_type: str = ""      # source-side type string, for debugging
    fitid: str = ""         # stable hash for dedup
    memo: str = ""


class AdapterError(Exception):
    pass


# ---------- shared helpers ----------

def _parse_date(s: str) -> Optional[date]:
    s = (s or "").strip().strip('"').strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None  # unknown date -> caller skips row


def _money(s: str) -> Optional[float]:
    """Parse '$1,234.56', '(12.00)', '-12', '' -> float or None."""
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace("$", "").replace(" ", "")
    if s == "" or s == "-":
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _rows(text: str) -> list[dict]:
    # sniff dialect; fall back to excel
    text = text.lstrip("\ufeff")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return list(csv.DictReader(io.StringIO(text), dialect=dialect))


def _header(row: dict) -> set[str]:
    return {(k or "").strip().lower() for k in row.keys()}


def _get(row: dict, *names: str) -> str:
    """Case/space-insensitive column get."""
    want = {n.strip().lower() for n in names}
    for k, v in row.items():
        if (k or "").strip().lower() in want:
            return (v or "").strip()
    return ""


# ---------- adapters ----------

class BaseAdapter:
    name = "base"
    label = "Unknown"

    @staticmethod
    def matches(header: set[str], sample: list[dict]) -> bool:
        raise NotImplementedError

    @staticmethod
    def parse(text: str, account_label: str) -> list[NormalizedTxn]:
        raise NotImplementedError


class CapitalOneAdapter(BaseAdapter):
    """Account Number,Transaction Description,Transaction Date,Transaction Type,Transaction Amount,Balance"""
    name = "capital_one"
    label = "Capital One"

    @staticmethod
    def matches(header, sample):
        # M&T with header is a superset; exclude it from matching here by requiring
        # the absence of M&T's distinctive 'transaction date' column.
        if "transaction date" in header:
            return False
        return ({"transaction description", "transaction type"} <= header
                and "transaction amount" in header and "balance" in header)

    @staticmethod
    def parse(text, account_label):
        out = []
        for r in _rows(text):
            ttype = _get(r, "Transaction Type").lower()
            amt = _money(_get(r, "Transaction Amount"))
            if amt is None:
                continue
            # Credit = money in, Debit = money out
            if ttype == "debit":
                amt = -abs(amt)
            elif ttype == "credit":
                amt = abs(amt)
            d = _parse_date(_get(r, "Transaction Date"))
            if not d:
                continue
            out.append(NormalizedTxn(
                date=d, description=_get(r, "Transaction Description"),
                amount=round(amt, 2), account=account_label, bank="capital_one",
                raw_type=ttype,
            ))
        return out


class CitizensAdapter(BaseAdapter):
    """Quoted CSV: Transaction Type,Date,Account Type,Description,Amount,Reference No.,Credits,Debits"""
    name = "citizens"
    label = "Citizens Bank"

    @staticmethod
    def matches(header, sample):
        return {"transaction type", "credits", "debits"} <= header

    @staticmethod
    def parse(text, account_label):
        out = []
        for r in _rows(text):
            amt = _money(_get(r, "Amount"))
            if amt is None:
                amt = _money(_get(r, "Credits")) or (
                    -abs(_money(_get(r, "Debits")) or 0) if _get(r, "Debits") else None)
            if amt is None:
                continue
            # Citizens has its own TRANSFER type; honor sign as given otherwise
            d = _parse_date(_get(r, "Date"))
            if not d:
                continue
            out.append(NormalizedTxn(
                date=d, description=_get(r, "Description"),
                amount=round(amt, 2), account=account_label, bank="citizens",
                raw_type=_get(r, "Transaction Type"),
            ))
        return out


class GeminiAdapter(BaseAdapter):
    """Reference Number,Transaction Post Date,Description of Transaction,Transaction Type,Amount
    Gemini: positive = charge (expense => money out), negative = payment/return (money in).
    """
    name = "gemini"
    label = "Gemini Credit Card"

    @staticmethod
    def matches(header, sample):
        return {"reference number", "transaction post date", "description of transaction"} <= header

    @staticmethod
    def parse(text, account_label):
        out = []
        for r in _rows(text):
            amt = _money(_get(r, "Amount"))
            if amt is None:
                continue
            out.append(NormalizedTxn(
                date=_parse_date(_get(r, "Transaction Post Date")),
                description=_get(r, "Description of Transaction"),
                amount=round(-amt, 2),  # invert: charge -> negative
                account=account_label, bank="gemini",
                raw_type=_get(r, "Transaction Type"),
            ))
        return [t for t in out if t.date]


class ChaseAdapter(BaseAdapter):
    """Transaction Date,Post Date,Description,Category,Type,Amount,Memo
    Chase: negative = Sale (expense), positive = Return/Payment (inflow). Sign already canonical.
    """
    name = "chase"
    label = "Chase Credit Card"

    @staticmethod
    def matches(header, sample):
        return {"post date", "description", "type"} <= header and "memo" in header

    @staticmethod
    def parse(text, account_label):
        out = []
        for r in _rows(text):
            amt = _money(_get(r, "Amount"))
            if amt is None:
                continue
            d = _parse_date(_get(r, "Transaction Date"))
            if not d:
                continue
            out.append(NormalizedTxn(
                date=d, description=_get(r, "Description"),
                amount=round(amt, 2), account=account_label, bank="chase",
                raw_type=_get(r, "Type"), memo=_get(r, "Memo"),
            ))
        return out


class MTHeaderAdapter(BaseAdapter):
    """M&T with header: Account Number,Transaction Description,Transaction Date,Transaction Type,Transaction Amount,Balance
    M&T: negative = outflow, positive = inflow. Sign canonical already.
    """
    name = "mt"
    label = "M&T Bank"

    @staticmethod
    def matches(header, sample):
        if not ("transaction description" in header and "balance" in header):
            return False
        # guard: headerless M&T export puts DATA in the "header" slot; real one has a date-looking description col
        return "transaction date" in header

    @staticmethod
    def parse(text, account_label):
        out = []
        prev_balance = None
        for r in _rows(text):
            dval = _get(r, "Transaction Date")
            desc = _get(r, "Transaction Description")
            # Some M&T exports ship a header that does not match the data column order
            # (Date and Description swapped). Detect and repair by testing which value parses as a date.
            d = _parse_date(dval)
            if d is None:
                d2 = _parse_date(desc)
                if d2 is not None:
                    d, desc, dval = d2, dval, desc
            amt = _money(_get(r, "Transaction Amount"))
            bal = _money(_get(r, "Balance"))
            raw_type = _get(r, "Transaction Type")
            # If amount came back empty but 'type' holds a number and vice-versa, swap them too.
            if amt is None:
                maybe = _money(raw_type)
                if maybe is not None:
                    amt, raw_type = maybe, ""
            if d is None:
                continue
            if amt is None and bal is not None and prev_balance is not None:
                amt = round(bal - prev_balance, 2)
            if bal is not None:
                prev_balance = bal
            if amt is None:
                continue
            # If a Type column says Debit/Credit, honor it over raw sign ambiguity.
            t = (raw_type or "").strip().lower()
            if t == "debit":
                amt = -abs(amt)
            elif t == "credit":
                amt = abs(amt)
            out.append(NormalizedTxn(
                date=d, description=desc, amount=round(amt, 2),
                account=account_label, bank="mt", raw_type=raw_type,
            ))
        return out


class MTHeaderlessAdapter(BaseAdapter):
    """The 'export' file: same M&T layout but NO header row.
    Cols: idx,date,description,amount,balance  (6 cols, first is a row number)
    """
    name = "mt"
    label = "M&T Bank"

    @staticmethod
    def matches(header, sample):
        """The 'export' file: same M&T layout but NO header row.
        Cols: [row#, date, description, amount, balance] (+ optional trailing col).
        Detect by inspecting the raw key names of the first parsed row.
        """
        if not sample:
            return False
        keys = [(k or "").strip() for k in sample[0].keys()]
        if len(keys) < 5:
            return False
        return (bool(re.match(r"^\d+$", keys[0]))
                and bool(re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", keys[1]))
                and bool(re.match(r"^-?\d+\.?\d*$", keys[3] or "")))

    @staticmethod
    def parse(text, account_label):
        """Headerless M&T export. Rows: [idx, date, desc, outflow, inflow, balance, (extra)].

        Column roles (VERIFIED against real exports):
          index 3 = OUTFLOW amount  (negative, BLANK when money came in)
          index 4 = INFLOW amount   (positive, BLANK when money went out)
          index 5 = running BALANCE (its own sign convention: negative = overdrawn,
                    positive = normal). NEVER derive amounts from this column.

        So the amount is whichever of cells 3/4 is populated; a blank cell is NOT zero
        and does NOT inherit a neighbour's value.

        IMPORTANT: rows are NOT guaranteed to be strictly chronological (deposits can
        appear out of file order relative to card charges). Deriving the amount from a
        balance delta mislabels deposits as whatever postings happen to precede them in
        file order. Parse the amount cells positionally instead.

        Layouts tolerated:
          [idx, date, desc, outflow, inflow, balance]        (5/6 cols)
          [idx, date, desc, outflow, inflow, '',  balance]  (trailing cols ignored)
        """
        text = text.lstrip("\ufeff")
        reader = csv.reader(io.StringIO(text))
        out = []
        for row in reader:
            if len(row) < 5:
                continue
            if not re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", (row[1] or "").strip()):
                continue
            d = _parse_date(row[1])
            if d is None:
                continue
            desc = row[2]

            # Amount is whichever of the two amount cells (idx 3 = outflow,
            # idx 4 = inflow) is populated. Blank means "not this direction",
            # NOT zero-by-inheritance.
            outflow = _money(row[3]) if len(row) > 3 else None
            inflow = _money(row[4]) if len(row) > 4 else None
            if outflow is None and inflow is None:
                # Both cells blank -> no amount information; skip rather than emit 0.
                continue
            amount = outflow if outflow is not None else inflow

            out.append(NormalizedTxn(
                date=d, description=desc, amount=round(amount, 2),
                account=account_label, bank="mt",
            ))
        return out


# ordered most-specific first
ADAPTERS = [
    CitizensAdapter,
    GeminiAdapter,
    ChaseAdapter,
    MTHeaderAdapter,       # M&T with header (has 'transaction date' + 'transaction amount')
    CapitalOneAdapter,
    MTHeaderlessAdapter,   # pseud-header row-number detection (last resort)
]


def detect_adapter(text: str, sample_override: list[dict] | None = None):
    sample = sample_override if sample_override is not None else _rows(text)[:5]
    header = _header(sample[0]) if sample else set()
    for a in ADAPTERS:
        try:
            if a.matches(header, sample):
                return a
        except Exception:
            continue
    return None


def parse_any(text: str, account_label: str) -> tuple[list[NormalizedTxn], str]:
    a = detect_adapter(text)
    if a is None:
        raise AdapterError("Could not detect bank format for this CSV")
    return a.parse(text, account_label), a.label
