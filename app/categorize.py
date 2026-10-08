"""Rule-based categorization. No LLM.

Two matching layers:
  1. Learned rules (from manual edits) — highest priority.
  2. Built-in seed rules (regex/substring against the cleaned merchant string).
Fallback: Uncategorized.
"""
from __future__ import annotations

import re

UNCATEGORIZED = "uncategorized"
TRANSFER = "transfer"

# Seed taxonomy. id -> {name, kind, color}
DEFAULT_CATEGORIES = [
    {"id": "income",        "name": "Income",        "kind": "income",   "color": "#22c55e"},
    {"id": "groceries",     "name": "Groceries",     "kind": "expense",  "color": "#84cc16"},
    {"id": "dining",        "name": "Dining",        "kind": "expense",  "color": "#f97316"},
    {"id": "gas",           "name": "Gas/Fuel",      "kind": "expense",  "color": "#eab308"},
    {"id": "utilities",     "name": "Utilities",     "kind": "expense",  "color": "#06b6d4"},
    {"id": "housing",       "name": "Rent/Housing",  "kind": "expense",  "color": "#8b5cf6"},
    {"id": "insurance",     "name": "Insurance",     "kind": "expense",  "color": "#64748b"},
    {"id": "subscriptions", "name": "Subscriptions", "kind": "expense",  "color": "#ec4899"},
    {"id": "health",        "name": "Health",        "kind": "expense",  "color": "#14b8a6"},
    {"id": "entertainment", "name": "Entertainment", "kind": "expense",  "color": "#a855f7"},
    {"id": "shopping",      "name": "Shopping",      "kind": "expense",  "color": "#3b82f6"},
    {"id": "transfer",      "name": "Transfer",      "kind": "transfer", "color": "#94a3b8"},
    {"id": "fees",          "name": "Fees",          "kind": "expense",  "color": "#ef4444"},
    {"id": "cash",          "name": "Cash/ATM",      "kind": "expense",  "color": "#78716c"},
    {"id": "uncategorized", "name": "Uncategorized", "kind": "expense",  "color": "#525252"},
]

# ---- merchant cleaning: strip store numbers, locations, txn suffixes ----
_STRIP_PATTERNS = [
    re.compile(r"\b\d{2}/\d{2}\b.*$"),                 # trailing dates
    re.compile(r"\b\d{5,}\b"),                          # long numbers (#2038829132, ref)
    re.compile(r"\bXXXX+\d*\b", re.I),                  # masked accts
    re.compile(r"#\s*\d+"),                             # #121
    re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"),   # phone numbers
    re.compile(r"\b(?:\d{3,5}\s+)?[A-Z]{0,3}\d?[A-Z]*\s*(?:ST|RD|AVE|BLVD|POST RD|HWY|DR|LN)\b.*$", re.I),
    re.compile(r"\b(MILFORD|MONROE|STRATFORD|BRIDGEPORT|HARTFORD|NASHUA|TRUMBULL|WESTPORT|USA|CT|NH|NY|MI|NC)\b.*$", re.I),
    re.compile(r"\bG\d{5}\b"),                          # M&T txn ids
    re.compile(r"[?*]+$"),
    re.compile(r"\s{2,}"),
]

# leading noise tokens the banks prepend
_LEAD_STRIP = re.compile(
    r"^(WEB PMT|PIN|POS DEBIT|POS |ATM DEBIT|ATM WITHDRAWAL|ATM |DEBIT CARD |SQ \*|TST\*|PAYPAL \*|PAYPAL |"
    r"Check \d+|TOPPER\*|PAYRIX|NON MT )",
    re.I,
)
# Amazon order-id suffix: 'AMAZON MKTPL*5344S4V92', 'Amazon.com*5Q1Z559Q1', 'AMAZON DIGI*ABC'.
# Normalize to a stable 'Amazon' merchant so the shopping rule matches.
_AMZ_RE = re.compile(r"^(AMAZON(?:\.COM)?(?:\s+MKTPL|\s+MARK|\s+DIGI)?)\*?.*$", re.I)


def clean_merchant(desc: str) -> str:
    s = (desc or "").strip()
    raw = s
    # collapse all Amazon variants (MKTPL/DIGI/MARK/.com + order id) to one merchant
    amz = _AMZ_RE.match(s)
    if amz:
        return "Amazon"
    s = _LEAD_STRIP.sub("", s)
    # split on common separators: keep the first meaningful chunk
    for pat in _STRIP_PATTERNS:
        s = pat.sub(" ", s)
    s = re.sub(r"[^A-Za-z0-9&'\-\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -")
    s = re.sub(r"\s+(LLC|INC|CO|CORP)$", "", s, flags=re.I)
    s = s.strip()
    if not s:
        # fall back: strip only numbers/punct from the raw description
        s = re.sub(r"[^A-Za-z&'\-\s]", " ", raw)
        s = re.sub(r"\s+", " ", s).strip(" -")
    return s


# ---- transfer detection (independent of taxonomy rules) ----
# NOTE: VENMO is intentionally NOT a blanket transfer pattern. It is used both
# for real money-moves AND for purchases (e.g. paying a swim instructor), so it
# must fall through to normal categorization. Only an explicit 'VENMO CASHOUT'
# is treated as a transfer.
_TRANSFER_PATTERNS = [
    r"\bZELLE\b", r"\bVENMO\s+CASHOUT\b", r"\bCASHOUT\b", r"\bCARD ?PYMT\b", r"CARD PYMT",
    r"\bCARDPYMT\b", r"PAYMENT THANK YOU", r"\bBILL PAY\b", r"CREDIT LINE TRANSFER",
    r"FROM SAVINGS", r"SCHEDULED TRANSFER", r"WITHdrawal TO \d+ CHECKING",
    r"WITHDRAWAL TO \d+", r"DEPOSIT FROM .*PAYROLL", r"ONLINE TRANSFER", r"\bTRANSFER\b",
    r"WEB PMT (?:M&T|CHASE|WELLS|CITIZENS|GEMINI)", r"WEB PMT LOAN", r"M&T #",
    r"CITIZENSBANK NA CARD PYMT", r"WELLS FARGO CARD", r"^MOBILE DEPOSIT",
]
_TRANSFER_RE = re.compile("|".join(_TRANSFER_PATTERNS), re.I)

# income signals
_INCOME_PATTERNS = [
    r"PAYROLL", r"DIRECT DEP", r"INTEREST (?:PAID|REVERSAL)\b", r"INTEREST EARNED",
    r"REWARD REDEMPTION",
    r"REBATE", r"REFUND", r"MOBILE DEPOSIT", r"ZELLE P2P PAYMENT RECEIVED",
    r"DEPOSIT FROM", r"^DEPOSIT", r"PURCHASE_RETURN", r"\bRETURN\b",
]
# descriptions that look like income patterns but are actually charges
_INCOME_DENY = re.compile(r"INTEREST CHARGE|\bCHARGE\b|\bFEE\b|PURCHASE INTEREST", re.I)


def is_transfer(desc: str, raw_type: str = "", amount: float = 0.0) -> bool:
    t = (raw_type or "").upper()
    if t in ("TRANSFER", "PAYMENT", "PAYMENT_TRANSACTION", "CREDIT LINE TRANSFER-M&T BANK"):
        # Citizens TRANSFER rows, Chase Payment rows, Gemini payment_transaction
        if t == "PAYMENT_TRANSACTION":
            return True
        if t == "PAYMENT":
            return True
        if t == "TRANSFER":
            return True
    # Incoming Zelle "PAYMENT RECEIVED" is real money-in (e.g. the renter), NOT an
    # internal move — let it fall through to income detection. Outgoing Zelle
    # (payments SENT) and self-credits ("ZELLE CREDIT") stay transfers.
    if re.search(r"ZELLE", desc or "", re.I) and re.search(r"RECEIVED", desc or "", re.I):
        return False
    return bool(_TRANSFER_RE.search(desc or ""))


# ---- seed rules: regex -> category id (on cleaned merchant, lowercased) ----
SEED_RULES: list[tuple[str, str]] = [
    # groceries
    (r"big y|cumberland farms|stop & shop|shoprite|shop rite|trader joe|whole foods|aldi|"
     r"price chopper|stewarts|walgreens|aldi|costco|sam'?s club|stevenson|bjs wholesale", "groceries"),
    # dining
    (r"uber ?eats|doordash|grubhub|mcdonald|starbucks|dunkin|chipotle|taco bell|wendy|"
     r"burger king|subway|panera|domino|pizza|seeley|caf|deli|restaurant|flipside|silver sands|"
     r"gyro|bagel|devines|brew|tribus|bar |thirtythree|coffee|maheswara|fire and dice|jangs|"
     r"consign|concession", "dining"),
    # gas
    (r"exxon|mobil|citgo|shell|sunoco|chevron|valero|speedway|cumberland farms|plains road|"
     r"\bpetro\b|valvoline|cumberland", "gas"),
    # utilities
    (r"united illuminating|ui |southern connecticut gas|regional water|optimum|"
     r"t-mobile|tmobile|tmo|verizon|at&t|comcast|frontier|eversource|aquarion", "utilities"),
    # housing
    (r"lexington green|on the mark|mangament|management|\brent\b|mortgage|milford|city of|tax co|\bproperty\b|"
     r"duck pond|service finance|scheduled transfer", "housing"),
    # insurance
    (r"national general|travelers|geico|progressive|state farm|allstate|insurance|"
     r"northwestern mu|northwestern mutual", "insurance"),
    # subscriptions
    (r"netflix|crunchyroll|spotify|hulu|disney\+|prime video|peacock|paramount|uber(?! ?eats)|"
     r"elevenlabs|midjourney|ledger|shopify|openai|anthropic|icloud|apple\.com|"
     r"roblox|adobe|notion|github|google (?:one|storage|workspace)|g ?suite|payrix|rockefeller", "subscriptions"),
    # health
    (r"pharmacy|cvs|rite aid|petco|pet choice|choice pet|\bvet\b|vet |dental|dentist|clinic|hospital|"
     r"medical|urgent care|walgreens", "health"),
    # entertainment
    (r"cinemark|amc|regal|theater|theatre|steam|playstation|xbox|nintendo|game|"
     r"retro|metal gear|powder ridge|janngs|jangs|amusement|ticketmaster|concert", "entertainment"),
    # shopping
    (r"amazon|target|walmart|best buy|barnes & noble|home depot|lowe'?s|macy|kohl|"
     r"tj maxx|marshalls|old navy|gap|nike|adidas|etsy|ebay|wine|spirits|total wine|"
     r"best ?buy|ups |fedex|usps|convention center|connecticut|retroworld|super retro|"
     r"metal gear|vending|nayax|crossmark", "shopping"),
    # fees
    (r"atm fee|service charge|overdraft|interest charge|fee\b|annual fee|late fee", "fees"),
    # cash
    (r"atm withdrawal|non mt atm|cash withdrawal|\batm\b", "cash"),
]

_SEED_COMPILED = [(re.compile(p, re.I), cid) for p, cid in SEED_RULES]


def categorize(description: str, cleaned: str, learned: dict[str, str] | None = None,
               raw_type: str = "", amount: float = 0.0) -> tuple[str, str]:
    """Return (category_id, source). source in: learned|transfer|income|rule|uncategorized."""
    # 1. transfers win
    if is_transfer(description, raw_type, amount):
        return TRANSFER, "transfer"

    # 2. learned manual rules (keyed on cleaned merchant, lowercase)
    if learned:
        hit = learned.get(cleaned.lower())
        if hit:
            return hit, "learned"

    # 2b. explicit charge/fee phrases win over (mis)income guesses — check the RAW description
    if re.search(r"PURCHASE INTEREST|INTEREST CHARGE|FINANCE CHARGE|CASH ADVANCE FEE|\bLATE FEE\b|ANNUAL (?:MEMBERSHIP )?FEE|ANNUAL FEE|OVERDRAFT|SERVICE CHARGE|ATM FEE", description or "", re.I):
        return "fees", "rule"

    # 3. income signals (skip anything that's actually a charge/fee)
    if not _INCOME_DENY.search(description or ""):
        for p in _INCOME_PATTERNS:
            if re.search(p, description or "", re.I):
                return "income", "income"

    # 4. seed rules on cleaned merchant
    hay = cleaned or description or ""
    for rx, cid in _SEED_COMPILED:
        if rx.search(hay):
            return cid, "rule"

    return "uncategorized", "uncategorized"
