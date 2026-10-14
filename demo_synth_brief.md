# TASK: Fully synthetic demo.json for the HOSTED Nestworth demo (docs/ only)

## Goal
Replace `finance-tracker/docs/demo.json` with a **100% fabricated, internally-consistent**
dataset. Right now it is a thin anonymization of Jaime's real finances — account names were
swapped but real amounts, real bills (Northwestern Mutual, Duck Pond daycare, United Illuminating,
National General), real merchants (Big Y, Wines More Wines, Blueforge), and real category mixes
remain. That is the problem. Nothing in the output may trace back to real life.

## HARD CONSTRAINTS
- Write ONLY to `finance-tracker/docs/demo.json`. Do NOT touch the local app DB
  (`finance-tracker/data/finance.db`) or the uvicorn app on port 8770.
- A backup already exists at `docs/demo.json.bak.*`. Keep it.
- Output must be valid JSON and match the EXACT schema below (same keys, same nesting,
  same types). The demo build reads this file client-side; a missing/renamed field breaks a tab.
- Deterministic: use a fixed random seed so regeneration is reproducible.
- Use `.venv/bin/python3` for any python runs (repo root is `finance-tracker/`).

## Deliverable
1. A generator script `finance-tracker/make_demo_synth.py` (new file) that produces the file.
2. Run it; produce `docs/demo.json`.
3. Validate: it parses as JSON; every top-level key present; every section's shape matches the spec.
4. Report back the list of fabricated account names + bill names you used.

## FABRICATION RULES (make it obviously fake but realistic)
- Account names: invent 6 generic-but-plausible ones (e.g. "Everyday Checking", "Main Savings",
  "Rewards Visa", "Travel Card", "Store Card", "Emergency Fund"). NO real bank names.
- Bills: invent 21 plausible recurring bills with generic names (Rent, Electric, Internet,
  Water, Auto Insurance, Phone, Streaming, Gym, Daycare, Student Loan, etc.). NO real providers.
- Merchants/transactions: invent plausible fake merchants (grocer "Harvest Market",
  "Bean & Leaf Coffee", "Metro Fuel", "Streamly", "Corner Pharmacy", etc.). No real brand names
  that map to Jaime.
- Amounts: entirely fabricated, plausible ranges. Rent ~1400-1650, utilities ~50-260,
  groceries ~180-650/mo, etc. Do NOT copy any figure from the current demo.json.
- Categories: KEEP the standard category taxonomy (income, housing, groceries, dining,
  transport, shopping, utilities, health, entertainment, subscriptions, insurance) — that's
  generic and fine. You may also add a couple custom ones (e.g. daycare, pets).
- Crypto: keep the same coin ids/symbols (bitcoin, ethereum, solana, etc.) — public market data.
  Fabricate a plausible price/market_cap/spark/chart series deterministically (random walk).
  Favorites/holdings can stay on public coins with fake amounts.
- AI costs, economy news, rates: fabricate plausible values; news titles can be generic
  (they are public RSS-style headlines, not personal).
- 3-month trend windows and months arrays must be internally consistent (series dates line up
  with the `months` lists; totals = sum of parts where the app expects that).

## SCHEMA (authoritative — match exactly)
See the extracted shape in this same directory: `demo_schema.txt` (already written). Also read
the current `docs/demo.json` for concrete value examples (the SHAPE is right; the VALUES must change).

Top-level keys and shapes:
- dashboard: {totals{income,expense,net,savings}, grade{grade,color,rate,net,income,expense,
  trend{direction,delta,prev_grade,prev_rate}, all_time{grade,color,rate,net,income,expense,savings},
  savings}, categories[11]{key,name,color,kind,total,count,hidden,avg_3mo,avg_active,series{month:val}},
  months[4]{month,income,expense,net}, accounts[6]{name,income,expense}, uncategorized{count,amount},
  range{start,end}}
- categories[11]: {id,key,name,kind,color,is_custom,hidden}
- accounts[6]: {id,name,bank,kind,exclude,cnt,first_date,last_date}
- budgets: {month, rows[24]{key,name,color,budget,spent,remaining,pct}, categories[32]{key,name,color},
  totals{budget,spent,spent_all,spent_unbudgeted}}
- trends: {months[4], categories[10]{key,name,color,total,avg_3mo,avg_active,series{}},
  month_series[4]{month,income,expense,net}, avg_3mo{income,expense,net}, avg_all{income,expense,net,months}}
- transactions: {total, items[45]{id,date,description,cleaned,amount,raw_type,category_source,memo,ckey,cname,ccolor,account}}
- rules[13]: {id,merchant_pattern,category_key,hits}
- imports[1]: {id,filename,account,bank,inserted,skipped,created_at}
- savings: {accounts[1]{id,name,bank,deposits,withdrawals,interest,net,count,last_date,series[]{date,description,amount,cat_key},balance,balance_date}, totals{deposits,withdrawals,interest,net,count,balance}}
- crypto: {markets[10]{id,symbol,name,image,price,market_cap,rank,chg_24h,chg_7d,spark[]},
  charts{bitcoin[],ethereum[],solana[]}, favorites[], holdings[]{id,coin_id,symbol,name,amount}, news[3]{title,url,source,category}}
- savingsHistory: {months[12]{month,balance}, current, current_month, txn_net}
- bills: {month, rows[21]{id,name,match_cleaned,expected_amount,due_day,kind,account_id,category_key,
  active,created_at,manual_paid_month,payments_per_month,status,paid_date,paid_amount,variance,manual,
  paid_count,expected_count,remaining_count,per_payment}, counts{paid,partial,due,overdue,total,expected,paid_amount,left}}
- paydown: {cards[3]{id,name,bank,balance,balance_date,apr,min_payment,payments_count,payments_total,payments_last,charges_total}, totals{owed,min,avg_apr,cards_with_balance,cards_total}}
- aiCosts: {total,count,first_date,last_date,daily[]{date,cost,n},daily_by_model[]{date,cost,n,model},monthly[]{month,cost,n},by_model[]{model,cost,n},by_wallet[]{wallet_type,cost,n},models[]}
- economyNews: {economy[8]{title,url,source,published}, bonds[4]{title,url,source,published}}
- economyMarkets: {rates{10y{label,value},5y{},2y{},13w{},10y2y{}}}
