"""PDF statement parsers.

These operate on the *text* extracted from a PDF (via pypdf), not on CSV. They are
invoked from the import endpoint when the uploaded file is a PDF, or when the text
looks like a known statement layout.

Sign convention (canonical, same as CSV adapters): amount > 0 = money IN,
amount < 0 = money OUT. Credit-card charges are therefore stored negative, and
payments/credits positive.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from . import NormalizedTxn, AdapterError


def _d(s: str) -> Optional[date]:
    s = (s or "").strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# A money token: $1,234.56 or ($100.00) or -12.34
_AMT_RE = re.compile(r"^\(?-?\$?\d[\d,]*\.\d{2}\)?$")
# Any date-looking token mm/dd/yy(yy)
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{2,4}$")


def _amt(tok: str) -> Optional[float]:
    tok = tok.strip()
    if not tok:
        return None
    neg = tok.startswith("(") and tok.endswith(")")
    t = tok.strip("()").replace("$", "").replace(",", "").strip()
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _clean_desc(raw: str) -> str:
    """Collapse a multi-line description into one tidy string."""
    s = re.sub(r"\s+", " ", raw).strip()
    return s


# --------------------------------------------------------------------------
# Gemini credit card statement
# --------------------------------------------------------------------------
# Layout (one transaction = 5..7 text lines):
#   6969147170            <- reference number
#   52                    <- some code
#   08/05/26              <- transaction date
#   08/06/26              <- post date
#   PETCO 1859   1401-1403 BOSTON POST RD      <- description line(s)
#           MILFORD  06460  CT USA
#   $11.91                <- amount
# Payments show as e.g.  "ACH PAYMENT" then  ($100.00)
# The trailing sections 'Fees' / 'Interest Charged' also carry dated rows.

_GEMINI_MARKERS = ("gemini credit card statement", "gemini constellation")


def looks_like_gemini_pdf(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in _GEMINI_MARKERS) and "transactions" in t


def parse_gemini_pdf(text: str, account_label: str) -> list[NormalizedTxn]:
    """Parse the Gemini credit-card statement text into normalized transactions.

    Strategy: walk lines; when we see a date line immediately followed by another
    date line, that pair is (transaction date, post date). Everything after until
    an amount-only token is the description; the amount token closes the record.
    """
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    # Keep only the transaction-bearing region(s): from first "Transactions"
    # header to the "Total Fees"/"YTD" noise at the end (we still want Fees +
    # Interest sections, so we stop at YTD summary).
    out: list[NormalizedTxn] = []

    i = 0
    n = len(lines)
    while i < n - 1:
        a = lines[i].strip()
        b = lines[i + 1].strip()
        if _DATE_RE.match(a) and _DATE_RE.match(b):
            txn_date = _d(a)
            if not txn_date:
                i += 1
                continue
            # collect description lines until we hit an amount-only token
            desc_parts: list[str] = []
            j = i + 2
            amt = None
            while j < n:
                tok = lines[j].strip()
                if _AMT_RE.match(tok):
                    amt = _amt(tok)
                    break
                # stop if we run into another date pair (missing amount -> bail)
                if (j + 1 < n and _DATE_RE.match(tok)
                        and _DATE_RE.match(lines[j + 1].strip()) and desc_parts):
                    break
                if tok:
                    desc_parts.append(tok)
                j += 1
            if amt is not None:
                desc = _clean_desc(" ".join(desc_parts))
                # Credit-card: positive charge = money OUT -> negative amount.
                out.append(NormalizedTxn(
                    date=txn_date, description=desc, amount=round(-amt, 2),
                    account=account_label, bank="gemini", raw_type="",
                ))
                i = j + 1
                continue
        i += 1

    # de-dup exact repeats (some statements repeat rows at page breaks)
    seen = set()
    deduped = []
    for t in out:
        k = (t.date, t.description, t.amount)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(t)
    return deduped


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

class PdfAdapterError(AdapterError):
    pass


_PDF_PARSERS = [
    (looks_like_gemini_pdf, parse_gemini_pdf, "Gemini Credit Card (PDF)"),
]


def detect_pdf_parser(text: str):
    for matcher, parser, label in _PDF_PARSERS:
        try:
            if matcher(text):
                return parser, label
        except Exception:
            continue
    return None, None


def parse_pdf_any(text: str, account_label: str) -> tuple[list[NormalizedTxn], str]:
    parser, label = detect_pdf_parser(text)
    if parser is None:
        raise PdfAdapterError("Unrecognized PDF statement format")
    txns = parser(text, account_label)
    if not txns:
        raise PdfAdapterError(f"{label}: no transactions found")
    return txns, label
