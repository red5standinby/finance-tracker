"""SQLite storage + schema."""
from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from typing import Iterable

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "finance.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    bank TEXT DEFAULT '',
    kind TEXT DEFAULT 'checking',   -- checking|savings|credit
    exclude INTEGER DEFAULT 0       -- 1 = ignore in totals (e.g. Warchest)
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT DEFAULT 'expense',    -- income|expense|transfer
    color TEXT DEFAULT '#64748b',
    is_custom INTEGER DEFAULT 0,
    hidden INTEGER DEFAULT 0        -- 1 = excluded from totals, charts, and lists
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    date TEXT NOT NULL,
    description TEXT NOT NULL,
    cleaned TEXT DEFAULT '',
    amount REAL NOT NULL,           -- + in, - out
    raw_type TEXT DEFAULT '',
    category_id INTEGER REFERENCES categories(id),
    category_source TEXT DEFAULT 'uncategorized',
    memo TEXT DEFAULT '',
    fitid TEXT NOT NULL,
    import_batch TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, fitid)
);

CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_txn_cat ON transactions(category_id);
CREATE INDEX IF NOT EXISTS idx_txn_acct ON transactions(account_id);

CREATE TABLE IF NOT EXISTS learned_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cleaned TEXT NOT NULL UNIQUE,   -- lowercase cleaned merchant
    category_key TEXT NOT NULL,
    hits INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS budgets (
    category_key TEXT PRIMARY KEY,   -- one monthly target per category
    amount REAL NOT NULL DEFAULT 0,  -- 0 or absent = no budget set
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS imports (
    id TEXT PRIMARY KEY,
    filename TEXT,
    account TEXT,
    bank TEXT,
    inserted INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


@contextmanager
def db():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _backup_once_per_day():
    """Copy the live DB to data/backups/ once per calendar day.

    Safety net so a bad migration or an accidental wipe never loses the user's
    custom categories, learned rules, or manual tags. Keeps the last 30 backups.
    """
    if not os.path.exists(DB_PATH):
        return
    bdir = os.path.join(os.path.dirname(DB_PATH), "backups")
    os.makedirs(bdir, exist_ok=True)
    stamp = date.today().isoformat()
    dest = os.path.join(bdir, f"finance-{stamp}.db")
    if os.path.exists(dest):
        return
    try:
        src = sqlite3.connect(DB_PATH); out = sqlite3.connect(dest)
        with out:
            src.backup(out)
        src.close(); out.close()
        backups = sorted(f for f in os.listdir(bdir) if f.startswith("finance-") and f.endswith(".db"))
        for old in backups[:-30]:
            os.remove(os.path.join(bdir, old))
    except Exception as e:  # never block startup on a backup failure
        print(f"[db] backup skipped: {e}")


def init_db(seed_categories: Iterable[dict]):
    _backup_once_per_day()
    with db() as con:
        con.executescript(SCHEMA)
        # migration: add hidden column to older DBs
        cols = {r[1] for r in con.execute("PRAGMA table_info(categories)")}
        if "hidden" not in cols:
            con.execute("ALTER TABLE categories ADD COLUMN hidden INTEGER DEFAULT 0")
        # seed built-in categories (INSERT OR IGNORE keeps any user edits/creations)
        for c in seed_categories:
            con.execute(
                "INSERT OR IGNORE INTO categories(key,name,kind,color,is_custom) VALUES(?,?,?,?,0)",
                (c["id"], c["name"], c["kind"], c["color"]),
            )
        # default Transfers to hidden ONCE (first time the transfer row is created),
        # tracked via a meta flag so a later user un-hide is never clobbered on restart.
        con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, val TEXT)")
        done = con.execute("SELECT val FROM meta WHERE key='transfer_hidden_default' ").fetchone()
        if not done:
            con.execute("UPDATE categories SET hidden=1 WHERE key='transfer'")
            con.execute("INSERT OR REPLACE INTO meta(key,val) VALUES('transfer_hidden_default','1')")


def make_fitid(account: str, d: date, amount: float, desc: str) -> str:
    h = hashlib.sha1(f"{account}|{d.isoformat()}|{amount:.2f}|{desc.strip().lower()}".encode()).hexdigest()
    return h[:20]
