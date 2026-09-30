# Finance Tracker

Local, multi-account finance dashboard. Upload bank CSVs → normalized, auto-categorized,
browsable + filterable, with inline manual retagging that learns.

## Quick start

```bash
cd /Users/jaime/.openclaw/workspace/finance-tracker
./serve.sh            # runs on http://0.0.0.0:8770
```

Open on the LAN: **http://<your-LAN-IP>:8770** (e.g. http://10.49.1.123:8770)

## Stack

- **Backend:** FastAPI + SQLite (WAL), Python 3.14, venv in `.venv/`
- **Frontend:** single-file vanilla JS SPA (`static/index.html`), dark theme
- **No LLM** — categorization is rule/regex based

## Supported bank formats (auto-detected)

| Bank | Notes |
|------|-------|
| M&T Bank (header) | Neg = outflow. Handles M&T's malformed header where Date/Description are swapped. |
| M&T Bank (headerless export) | Balance-delta sign inference; tolerates blank amount column. |
| Citizens Bank | Quoted CSV, separate Credits/Debits columns, `$` signs. |
| Chase Credit Card | Neg = Sale (expense), Pos = Return/Payment (inflow) — already canonical. |
| Gemini Credit Card | Positive = charge (expense), Negative = payment/return. Inverted on import. |
| Capital One | Explicit Debit/Credit type column. |

**Canonical sign convention everywhere:** `amount > 0` = money IN, `amount < 0` = money OUT.

## Categorization

1. **Explicit charges/fees** — `PURCHASE INTEREST`, `INTEREST CHARGE`, finance charges, late/annual
   fees, overdraft, service charge, ATM fee → `Fees` (checked on the raw description before income,
   so a credit-card *interest charge* isn't misread as interest *income*).
2. **Transfers first** — money moving between your own accounts (Zelle, Venmo, card payments,
   "Payment Thank You", "FROM SAVINGS", "Scheduled Transfer", payroll deposits into cash accounts)
   → tagged `Transfer`, **excluded** from income/expense totals.
3. **Learned rules** — created when you manually retag; keyed on the *cleaned merchant string*
   (store numbers / locations stripped), and retro-applied to other uncategorized rows.
4. **Income signals** — payroll, direct deposits, interest paid/earned, rewards, refunds.
5. **Seed rules** — ~14 category families on cleaned merchant.
6. **Fallback** — `Uncategorized`, flagged for manual review.

**Merchant normalization:** all Amazon variants (`AMAZON MKTPL*<id>`, `AMAZON DIGI*`, `AMAZON MARK*`,
`Amazon.com*<id>`) collapse to one `Amazon` merchant so they hit the shopping rule. `POST
/api/recategorize` re-runs cleaning + rules over every non-manual row, so improving the rules also
updates already-imported transactions (manual/learned tags are never touched).

### Category taxonomy (editable in-app)

Income · Groceries · Dining · Gas/Fuel · Utilities · Rent/Housing · Insurance · Subscriptions ·
Health · Entertainment · Shopping · Transfer · Fees · Cash/ATM · Uncategorized

Add your own via the **Rules → Categories** tab. Custom categories can be deleted
(their transactions fall back to Uncategorized); built-ins cannot.

## Features

- **Dashboard:** **financial grade card** (see below), income / expenses / net KPIs,
  spending-by-category bars, income-vs-expense monthly chart, per-account breakdown.
  Date-range + account filters, quick presets. Fully responsive (mobile-friendly).
- **Transactions:** search, date/category/account/source filters, column sorting, pagination,
  **inline category dropdown per row**. Picking a category opens an inline confirm bar with an
  **“Also apply to N similar transactions”** checkbox (N = other rows sharing the merchant);
  applying **learns a rule** and, if checked, retags all matches at once.
- **Rules:** view/delete learned merchant rules, manage categories, re-run the rules engine.
- **Import:** drag-drop CSV, auto bank detection, dedup on `(account, date, amount, cleaned desc)`,
  import history, per-account include/exclude toggle.

## Hiding categories (incl. Transfers)

Every category has a **hidden** flag. Hidden categories are **removed from totals, charts,
category lists, and the transactions view** — as if they don't exist — without deleting any data.

- **Transfers are hidden by default** (money moving between your own accounts isn't income or
  expense). This is the recommended setting.
- Toggle any category from **Rules → Categories** with the 👁 / 🙈 button.
- In **Transactions**, tick **Show hidden** to reveal hidden rows, or filter by the category
  directly (filtering a category always shows it).
- Hiding is non-destructive: unhide any time to bring the rows back.

## Trends tab

A dedicated **Trends** view for spotting patterns over time. Filters: date range, account, window
(6 / 12 / 24 months / all time), and a **view switcher**:

- **Expense mix (pie)** — donut of spend by category for the window, with a ranked legend
  (amount + % share). Slice tooltips show exact figures.
- **3-month averages** — table of each category's average *monthly* spend over the last 3 months,
  its all-time active-month average, total, and share of spend.
- **Spend vs income trend** — monthly income vs expense bars, with the 3-month averages called out.

KPI cards at the top always show 3-month average income / spend / net. Transfers and hidden
categories are excluded (they aren't real spend).

## Data safety

- **Built-in categories are seeded with `INSERT OR IGNORE`** — custom categories and any edits
  you make are never overwritten by an update or restart.
- Transfers are hidden **once** (tracked by a `meta` flag), so un-hiding them is sticky.
- The app auto-copies the DB to `data/backups/` **once per day** (keeps the last 30). A bad migration
  or accidental wipe therefore never loses your categories, rules, or manual tags.

## Account filter

Everywhere an account filter appears (Dashboard, Trends, Transactions) it's a **multi-select** —
tick any combination of accounts, or use **All** / **Clear**. The button stretches to fill its
filter slot. The backend takes a comma-separated `accounts` list for dashboard/trends (by id) and
transactions (by name).

**Selecting an account overrides its `exclude` flag.** By default flagged accounts (savings buckets
like War Chest) stay out of totals, but if you *explicitly* pick one in the filter you'll see its
activity — otherwise the filter would just show empty.

## PDF statements

`parse_capone_pdf.py` parses a Capital One 360 **multi-account PDF statement** into per-account
transactions (JSON). `POST /api/import/statement` then imports each sub-account as its own app
account. Idempotent on re-import. (CSV exports still go through the normal `/api/import` flow.)

## Grading

The dashboard grades any selected period by **savings rate** = `(income − expenses) / income`
(transfers excluded). A big colored letter card shows the grade, the rate, a **trend arrow**
vs the immediately preceding equal-length period, and the **all-time** grade.

| Grade | Savings rate |
|-------|--------------|
| A+ | ≥ 40% |
| A  | ≥ 30% |
| B  | ≥ 20% |
| C  | ≥ 10% |
| D  | ≥ 0% |
| F  | negative (spending more than earning) |

Edge cases: a period with spending but **no income** grades **F**; a period with no activity
grades **N/A**. Grades are computed for the *displayed range*, so switching the filter
(e.g. "This month") re-grades that window.

## API

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/import` | Upload CSV (`file`, `account`, `kind`, `exclude`) |
| POST | `/api/import/preview` | Detect format + preview rows without importing |
| GET  | `/api/dashboard` | Totals, **grade**, categories, months, accounts (`start`,`end`,`accounts`) |
| GET  | `/api/trends` | Per-category monthly series + 3-mo averages (`start`,`end`,`accounts`,`months`) |
| GET  | `/api/transactions` | Filter/sort/paginate (`include_hidden=true` to reveal hidden cats) |
| PATCH| `/api/transactions/{id}` | Retag (`category`, `learn`, `apply_to_similar`) |
| GET  | `/api/transactions/{id}/similar` | Same-merchant matches (count + sample) for the apply-to-similar UI |
| POST | `/api/import/statement` | Import a pre-parsed multi-account statement (each sub-account → its own account) |
| GET/POST/DELETE | `/api/categories` | Manage taxonomy |
| PATCH | `/api/categories/{key}` | Hide/show, rename, recolor (`hidden`, `name`, `color`) |
| GET/DELETE | `/api/rules` | Learned rules |
| GET  | `/api/accounts` | Accounts + txn counts/ranges |
| PATCH| `/api/accounts/{id}` | Rename / include-exclude |
| POST | `/api/recategorize` | Re-run rules over non-manual rows |
| GET  | `/api/imports` | Import history |

## Data

- DB: `data/finance.db` (SQLite, WAL). CSVs are **not** retained after parsing.
- `samples/` holds the originals used for development/testing.

## Auto-start (optional)

A launchd plist exists at `~/Library/LaunchAgents/com.chombi.finance-tracker.plist` but is
**not loaded** (a manual server is normally running). To enable boot-start:

```bash
launchctl load ~/Library/LaunchAgents/com.chombi.finance-tracker.plist
```
Unload with `launchctl unload …`.

## Tests

```bash
.venv/bin/python test_adapters.py    # smoke-test all 6 adapters + categorizer
```

## Roadmap (not built yet)

- Budgets per category with alerts
- Subscription/recurring detection
- Export back out (CSV / PDF reports)
- Multi-currency (USD-only for now)
