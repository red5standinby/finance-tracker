# How the Luna Family Finance Dashboard Works

A plain-English guide to what this app does, how your money flows through it, and
what every screen is showing you. Written for a family member who just wants to
open it and understand it — the technical appendix at the end is for whoever
maintains it.

---

## 1. What it is

A single, local web app that pulls together **every account** — checking, savings,
and credit cards — into one dashboard. You drop in a bank statement (CSV export or
a PDF), and the app:

1. **Normalizes** every transaction to one convention (see §4).
2. **Categorizes** it automatically using rules (no AI guessing — see §5).
3. **Shows** it on the Dashboard, Home, Trends, and Transactions screens.

It runs on your own machine at **http://<your-LAN-IP>:8770** — nothing is uploaded
anywhere. Your data lives in a single SQLite file on disk.

**Golden rule of the numbers:** money moving *between your own accounts* (a transfer,
a credit-card payment) is **not** income and **not** spending. The app tags those as
**Transfer** and hides them by default so your income/expense totals stay honest.

---

## 2. The screens (tabs, left sidebar)

### 🏠 Home — the 5-second glance
The "is everything okay?" screen. Big numbers up top, detail below.

- **Left to spend this month** — total monthly budgets minus what you've spent. If you
  haven't set budgets, it falls back to income-so-far minus spending.
- **This month net** — money in minus money out, with a delta vs. last month
  (`▲`/`▼`).
- **Cash position** — net flow across your checking/savings accounts. (Shown as *net
  flow* because live bank balances aren't synced yet.)
- **Recurring bills** — live summary from the **Bills** tab: “N paid · N due · N
  overdue,” with a link to the full list.
- **Budget health**, **Top spending this month**, and **Credit & savings** (savings
  rate, financial grade, and anything needing review).

### ◫ Dashboard — the full picture
Income / expense / net KPIs, the **financial grade card** (§7), spending-by-category
bars, an income-vs-expense monthly chart, a per-account breakdown, and category
budget bars. All of it respects the **date range** and **account** filters at the top.

### ↗ Trends — patterns over time
Spend vs. income by month, a category pie ("expense mix"), and 3-month averages per
category. Good for "where did the money actually go this quarter?"

### ◈ Savings — reserve-account view
Tracks deposit/withdrawal/interest activity for savings-style accounts (e.g. War
Chest), with a balance series over time.

### ₿ Crypto — portfolio tracker
Live prices for your manually-entered holdings. Add a holding (search any coin,
enter an amount), **edit** (✎) the amount, or delete (✕) it. Shows total value,
24h change, and a chart.

### ≡ Transactions — the ledger
Every transaction, searchable and filterable (date, category, account, amount,
"source"). You can **inline-retag** any row from its category dropdown — see §6.

### ▤ Bills — recurring payments at a glance
Every recurring bill in one list with its **status this month**: *paid* (with the
amount, the date, and a variance flag if it differed from usual), *due*,
*upcoming*, *overdue*, or *missed*. The app **detects** likely bills from your
history and offers them as **suggestions** — you confirm the real ones (and can
rename, set the expected amount, and set the typical due-day). Status updates
automatically the moment a matching payment posts; nothing to tick off by hand.

### ⚙ Rules — the brain
Manage **learned rules**, **categories** (add custom ones, rename, recolor, hide/unhide
with 👁/🙈), and re-run the categorization engine.

---

## 3. Accounts

Each bank/account is its own row with a **kind** (checking / savings / credit) and an
**exclude** flag. Excluded accounts (like a savings bucket) stay out of the headline
totals. **Explicitly selecting an account in a filter overrides its exclude flag** —
so you can always drill into one if you want.

---

## 4. The one sign convention (why numbers add up)

Every transaction is stored one way everywhere:

> **`amount > 0` = money IN · `amount < 0` = money OUT**

Banks disagree with each other (some show a purchase as positive, some negative), so
the importer flips each bank's format into this single rule. That's why a
**credit-card charge** shows as negative (money left) and a **payment to the card**
shows as positive (money returned to you / debt paid down).

---

## 5. How categorization works (no AI)

When a transaction is imported, it's scored against rules **in this order**:

1. **Fees first** — interest charges, late/annual fees, overdraft, ATM/service charges
   → `Fees`. (Checked before "income" so a credit-card *interest charge* isn't
   misread as interest *income*.)
2. **Transfers** — Zelle, auto-transfers, card payments, "Payment Thank You,"
   payroll deposits *into* cash accounts, etc. → `Transfer`, excluded from totals.
3. **Learned rules** — created when *you* retag something (see §6). Keyed on the
   cleaned merchant name.
4. **Income signals** — payroll, direct deposits, interest earned, refunds, rewards.
5. **Seed rules** — ~14 built-in category families on the cleaned merchant name.
6. **Fallback** — `Uncategorized`, flagged for you to review.

**Merchant cleaning:** store numbers and locations get stripped so all Amazon variants
collapse to one `Amazon` merchant, etc. This makes rules actually hit.

**Re-running:** improving the rules and hitting *re-run* in the Rules tab re-scores
every transaction **except** the ones you've manually tagged or that came from a
learned rule — those are never overwritten.

### Categories
Income · Groceries · Dining · Gas/Fuel · Utilities · Rent/Housing · Insurance ·
Subscriptions · Health · Entertainment · Shopping · Transfer · Fees · Cash/ATM ·
Uncategorized. Add your own in **Rules → Categories**.

### Hiding categories
Every category has a **hidden** flag. **Transfers are hidden by default** — moving your
own money isn't income or spending. Hidden categories vanish from totals, charts, and
lists **without deleting anything**; unhide any time. In Transactions, tick **Show
hidden** to peek.

---

## 6. Retagging that teaches the app

In **Transactions**, each row has a category dropdown. Pick a category and a small
confirm bar appears with a checkbox: **"Also apply to N similar transactions."**

- Applying stores a **learned rule** for that merchant, so future imports are
  auto-tagged.
- If you tick the box, it also sweeps every other transaction sharing that merchant in
  one go.
- Anything you tag by hand is marked **manual** and is never auto-overwritten.

---

## 7. The financial grade

For any date range, the app computes **savings rate = (income − expenses) ÷ income**
(transfers excluded) and shows a big letter:

| Grade | Savings rate |
|-------|--------------|
| A+ | ≥ 40% |
| A  | ≥ 30% |
| B  | ≥ 20% |
| C  | ≥ 10% |
| D  | ≥ 0% |
| F  | negative (spending more than earning) |

A period with spending but no income grades **F**; a period with no activity grades
**N/A**. The card also shows a trend arrow vs. the previous equal-length period.

---

## 8. Importing statements

Open **+ Import** (top-right). Pick the account (type a new name or choose an
existing one), then drag in a file.

- **CSV exports** — auto-detected across M&T, Citizens, Chase, Gemini, and Capital One.
- **PDF statements** — Gemini credit-card PDFs are parsed natively (text extraction →
  transaction table).
- **Duplicates are skipped** on a fingerprint of (account, date, amount, description),
  so re-importing the same file is safe.

> ⚠️ **Watch for overlap.** If an account was already loaded from a different source
> (e.g. a full-year CSV export) and you now import the monthly PDF, the descriptions
> and dates may differ enough that the dedupe doesn't catch them, and you can get
> duplicates. When in doubt, preview first and check the count.

---

## 9. Budgets

Set a monthly target per expense category (Dashboard → "set budget," or the budgets
API). Bars turn **green (<80%), amber (80–99%), red (≥100%)**. The Home screen rolls
these into "Budget remaining" and a health badge.

## 9b. Bills

The **Bills** tab tracks recurring payments (utilities, insurance, loan, daycare,
subscriptions) and whether each is paid this month.

- **Detection** is rule-based: a merchant that recurs across **≥3 distinct months**
at a **stable amount** (low variance) becomes a *suggestion*. You confirm the real
ones; suggestions you ignore never appear on the dashboard. Card-kind merchants
aren't suggested (add them by hand if you want them tracked).
- **Manual add:** the **search bar** on the Bills tab finds any transaction by
merchant; one click turns it into a bill (pulling its amount and typical day).
- Each bill stores a name, the merchant it matches (**substring** — so fragmented
variants like `NETFLIX 1` / `NETFLIX COM …` all count), an **expected amount**, and
  a **typical due-day** (inferred from history, editable).
- **Status** is computed live from transactions: **paid** (shows the actual amount,
  the date, and a `± vs usual` flag if it differed), **due**, **upcoming**,
  **overdue** (past due-day + 3 days), or **missed** (a past month that never posted).
- **Due-date progress bar** on each unpaid bill shows how far into the month we are
  toward its due-day, with a tick at the due date — so the list reads "what's due
  first" at a glance.
- Nothing to tick by hand — the moment a matching payment posts from any import, the
  bill flips to **paid**.

API: `GET /api/bills?month=YYYY-MM`, `GET /api/bills/candidates`,
`POST /api/bills`, `PATCH /api/bills/{id}`, `DELETE /api/bills/{id}`.

---

## 10. Data safety

- The DB (**`data/finance.db`**) auto-backs-up to `data/backups/` once a day (keeps 30).
- Built-in categories use `INSERT OR IGNORE`, so your custom categories and edits are
  **never** clobbered on restart or update.
- Hiding is non-destructive.
- **Never delete the DB** — it holds your custom categories and manual tags.

---

## 11. Serving it

```bash
cd /Users/jaime/.openclaw/workspace/finance-tracker
./serve.sh          # http://0.0.0.0:8770  →  open http://<LAN-IP>:8770
```

The server has **no auto-reload** — after editing backend code, restart it:

```bash
pkill -f "uvicorn app.main:app"; sleep 2
nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8770 >> server.log 2>&1 &
```

---

# Technical appendix

## Stack
FastAPI + SQLite (WAL) backend · single-file vanilla-JS SPA (`static/index.html`) ·
Python 3.14 venv in `.venv/`. **No LLM** anywhere in categorization.

## Data model (SQLite)
- **accounts** — `id, name, bank, kind, exclude, balance, balance_date`
- **categories** — `id, key, name, kind(income|expense|transfer), color, is_custom, hidden`
- **transactions** — `id, account_id, date, description, cleaned, amount(+in/-out),
  raw_type, category_id, category_source, memo, fitid, import_batch, created_at`
  — `UNIQUE(account_id, fitid)` is the dedupe guard.
- **learned_rules** — `cleaned, category_key, hits, updated_at` (one per merchant)
- **crypto_holdings / crypto_favorites** — portfolio tracker
- **budgets** — `category_key, amount` (one monthly target per category)
- **bills** — `name, match_cleaned, expected_amount, due_day, kind, active` (recurring
  payments + paid/due tracking; `match_cleaned` UNIQUE)
- **imports** — history: `id, filename, account, bank, inserted, skipped, created_at`

## Code map
- `app/main.py` — FastAPI app + all endpoints (import, dashboard, trends,
  transactions, categories, rules, budgets, crypto, accounts).
- `app/db.py` — schema, connection, `make_fitid`.
- `app/categorize.py` — cleaning + rule engine + `_TRANSFER_PATTERNS`.
- `app/adapters/__init__.py` — CSV adapters (M&T header + headerless, Citizens, Chase,
  Gemini, Capital One) + auto-detect.
- `app/adapters/pdf.py` — PDF statement parsers (Gemini CC).
- `static/index.html` — the whole front end.
- `build_pages.py` — builds the static `docs/` copy for hosting.

## Key mechanism: `make_fitid`
Dedupe fingerprint = sha1 of `account | date | amount | normalized-raw-description`
(lowercased, whitespace-collapsed, trailing artifacts stripped). **Hashes the RAW
description, not the cleaned one** — cleaning can change over time and must not
silently break dedupe.

## Source of truth on disk
- DB: `data/finance.db` · backups: `data/backups/`
- Dev sample statements: `samples/`
- CSVs are **not** retained after parsing.

## Tests / smoke checks
```bash
.venv/bin/python test_adapters.py        # all adapters + categorizer
```
