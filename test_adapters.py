#!/usr/bin/env python3
"""Smoke-test all adapters + categorizer against the real sample CSVs."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.adapters import parse_any, detect_adapter
from app.categorize import clean_merchant, categorize

CASES = [
    ("samples/mt_header.csv", "M&T Checking"),
    ("samples/mt_export.csv", "M&T Checking"),
    ("samples/citizens.csv", "Citizens Checking"),
    ("samples/chase.csv", "Chase Mastercard"),
    ("samples/gemini.csv", "Gemini Credit Card"),
    ("samples/capitalone.csv", "Capital One Warchest"),
]

for path, acct in CASES:
    text = open(path, encoding="utf-8-sig").read()
    det = detect_adapter(text)
    txns, label = parse_any(text, acct)
    print(f"\n=== {os.path.basename(path)} -> {label} ({len(txns)} txns) ===")
    for t in txns[:6]:
        cleaned = clean_merchant(t.description)
        ck, src = categorize(t.description, cleaned, None, t.raw_type, t.amount)
        print(f"  {t.date} {t.amount:>10.2f}  [{ck:<13} {src:<13}] {cleaned[:38]!r}")
    # sanity: show sign distribution
    ins = sum(1 for t in txns if t.amount > 0)
    outs = sum(1 for t in txns if t.amount < 0)
    print(f"  -> money in: {ins}, out: {outs}")
