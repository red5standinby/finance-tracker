"""One-off import of the Citizens Credit Card text dump into finance-tracker.

Reuses the app's own categorization engine + fitid/dedup so behavior matches
the CSV import path exactly. Creates the 'Citizens Credit Card' account.

Usage: python3 import_citizens_card.py <path-to-dump.txt>
"""
import sys
import uuid
from datetime import date

sys.path.insert(0, ".")

from app import categorize as cat
from app.db import db, make_fitid, init_db
from app.parse_citizens_card import parse_citizens_card_text
from app.main import ensure_account, learned_map, cat_by_key

SRC = sys.argv[1]
ACCT_NAME = "Citizens Credit Card"
BANK = "citizens"
KIND = "credit"

raw = open(SRC, encoding="utf-8").read()
txns = parse_citizens_card_text(raw)
print(f"parsed {len(txns)} transactions")

batch = uuid.uuid4().hex[:12]
inserted = skipped = 0

init_db(cat.DEFAULT_CATEGORIES)

with db() as con:
    acct_id = ensure_account(con, ACCT_NAME, BANK, KIND, exclude=False)
    print(f"account '{ACCT_NAME}' -> id {acct_id}")
    lm = learned_map(con)
    cats = cat_by_key(con)
    for t in txns:
        d = date(*t["date"])
        desc = t["description"]
        amt = t["amount"]
        cleaned = cat.clean_merchant(desc)
        fid = make_fitid(ACCT_NAME, d, amt, cleaned or desc)
        ckey, srcc = cat.categorize(desc, cleaned, lm, t["raw_type"], amt)
        if ckey not in cats:
            ckey, srcc = "uncategorized", "uncategorized"
        cid = cats[ckey]["id"]
        try:
            con.execute(
                """INSERT INTO transactions
                   (account_id,date,description,cleaned,amount,raw_type,category_id,
                    category_source,memo,fitid,import_batch)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (acct_id, d.isoformat(), desc, cleaned, amt, t["raw_type"], cid,
                 srcc, "", fid, batch),
            )
            inserted += 1
        except Exception as e:
            skipped += 1
    con.execute(
        "INSERT INTO imports(id,filename,account,bank,inserted,skipped) VALUES(?,?,?,?,?,?)",
        (batch, SRC.split("/")[-1], ACCT_NAME, BANK, inserted, skipped),
    )

print(f"inserted={inserted} skipped={skipped} batch={batch}")
