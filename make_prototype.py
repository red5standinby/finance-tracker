"""Mirror the hosted synthetic demo into prototype/ for a standalone shareable copy.

Since docs/demo.json is now FULLY synthetic (see make_demo_synth.py), the
prototype is simply a straight copy of the docs build — no extra renaming or
mangling needed. Earlier versions of this script re-fabricated names on top of
already-fake data, which produced junk like "Sample Bill 24".

Run: .venv/bin/python make_prototype.py
"""
from __future__ import annotations
import os, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "docs")
OUT = os.path.join(HERE, "prototype")
os.makedirs(OUT, exist_ok=True)

for f in ("demo.json", "index.html", "favicon.svg", ".nojekyll"):
    s = os.path.join(SRC, f)
    if os.path.exists(s):
        shutil.copyfile(s, os.path.join(OUT, f))

print("prototype/ mirrored from docs/ ->", sorted(os.listdir(OUT)))
