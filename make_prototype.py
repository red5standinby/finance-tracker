"""Generate a fully-fabricated (anonymized) dummy dataset for the LFFD prototype.

Reads the real Pages demo shape as a template, then replaces all names, banks,
merchant strings, holdings and figures with clearly fake values. Output goes to
prototype/demo.json and prototype/index.html (a copy of the DEMO build) so it can
be served standalone as a shareable, non-sensitive prototype.

Run: .venv/bin/python make_prototype.py
"""
from __future__ import annotations
import json, os, re, shutil, random

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DEMO = os.path.join(HERE, "docs", "demo.json")
SRC_HTML = os.path.join(HERE, "docs", "index.html")
OUT = os.path.join(HERE, "prototype")
os.makedirs(OUT, exist_ok=True)

d = json.load(open(SRC_DEMO))

# ---- fake bank/account identity ----
BANKS = {
    "M&T Checking": ("Northwind Bank", "Checking"),
    "Citizens Checking": ("Blue Harbor Bank", "Checking"),
    "Citizens Credit Card": ("Blue Harbor Bank", "Credit"),
    "Chase Mastercard": ("Vertex Card", "Credit"),
    "Gemini Credit Card": ("LumenCard", "Credit"),
    "War Chest": ("Reserve Fund", "Savings"),
}
NAME_MAP = {k: v[0] + " " + v[1] for k, v in BANKS.items()}

def fake_name(n: str) -> str:
    return NAME_MAP.get(n, n)

# ---- accounts ----
for a in d["accounts"]:
    if a.get("name") in NAME_MAP:
        newn = NAME_MAP[a["name"]]
        a["name"] = newn
        a["bank"] = BANKS[[k for k in NAME_MAP if NAME_MAP[k] == newn][0]][0]

# savings account names
for a in d["savings"].get("accounts", []):
    if a.get("name") in NAME_MAP:
        a["name"] = NAME_MAP[a["name"]]
        a["bank"] = "Reserve Fund"

d["savings"]["totals"].update(dict(deposits=24500.0, withdrawals=9800.0, interest=612.40,
                                   net=14700.0, count=1, balance=51300.0))

# ---- dashboard ----
d["dashboard"]["accounts"] = [{"name": fake_name(x["name"]) if x["name"] in NAME_MAP else "Everyday Checking",
                               "income": x["income"], "expense": x["expense"]}
                              for x in d["dashboard"]["accounts"]]
# keep totals but make them round/fake
d["dashboard"]["totals"] = {"income": 24500.0, "expense": 17250.0, "net": 7250.0}
g = d["dashboard"]["grade"]
g.update(income=24500.0, expense=17250.0, net=7250.0, rate=29.6, grade="A", color="#2fd6a0")
g["all_time"].update(income=147000.0, expense=103500.0, net=43500.0, rate=29.6, grade="A", color="#2fd6a0")
g["trend"].update(delta=2.1, prev_grade="B", prev_rate=27.5)
d["dashboard"]["uncategorized"] = {"count": 0, "amount": 0.0}

# ---- transactions: rewrite names/descriptions to fake merchants ----
FAKE_MERCHANTS = ["Acme Grocers", "Nimbus Coffee", "Pinnacle Fuel", "Byte Barn",
                  "Solstice Market", "Harbor Pharmacy", "Orbit Streaming", "Maple Diner",
                  "Cobalt Utilities", "Brightline Transit"]
rnd = random.Random(42)
for t in d["transactions"]["items"]:
    t["account"] = fake_name(t["account"]) if t["account"] in NAME_MAP else t["account"]
    m = FAKE_MERCHANTS[rnd.randrange(len(FAKE_MERCHANTS))]
    t["description"] = m + " #" + str(rnd.randrange(1000, 9999))
    t["cleaned"] = m.lower()
    if t.get("memo"):
        t["memo"] = ""

# ---- bills: rename daycare etc to generic ----
for b in d["bills"].get("rows", []):
    b["name"] = "Sample Bill " + str(b["id"])
    b["match_cleaned"] = "sample bill " + str(b["id"])
    b["account_id"] = None

# ---- crypto holdings: rename to showcase coins, fake amounts ----
CUT = {"bitcoin": 0.31, "ripple": 1500.0, "ethereum": 0.8, "sui": 210.0, "cardano": 4200.0}
for h in d["crypto"].get("holdings", []):
    cid = h.get("coin_id") or h.get("id")
    if cid in CUT:
        h["amount"] = CUT[cid]

json.dump(d, open(os.path.join(OUT, "demo.json"), "w"), indent=0)
shutil.copyfile(SRC_HTML, os.path.join(OUT, "index.html"))
# favicon + nojekyll
for f in ("favicon.svg", ".nojekyll"):
    s = os.path.join(HERE, "docs", f)
    if os.path.exists(s):
        shutil.copyfile(s, os.path.join(OUT, f))
print("prototype/ written ->", sorted(os.listdir(OUT)))
