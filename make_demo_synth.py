
import json
import random
from datetime import datetime, timedelta

# Fixed random seed for reproducibility
random.seed(42)

# --- Configuration and Data Definitions ---

# Start and end dates for data generation
START_DATE = datetime(2026, 1, 1)
END_DATE = datetime(2026, 9, 30) # Current month for demo is 2026-09
MONTHS_IN_DATA = 9 # Jan to Sep 2026
TREND_MONTHS = 4 # for dashboard, trends

# Fake account names
ACCOUNT_NAMES = [
    "Everyday Checking",
    "Growth Savings",
    "Rewards Visa",
    "Travel Platinum",
    "Store Loyalty Card",
    "Emergency Cash Fund"
]

# Bill name -> (category_key, kind) — deterministic, sensible mapping.
BILL_SPECS = {
    "Monthly Rent":            ("housing", "bill"),
    "Electricity Bill":        ("utilities", "bill"),
    "Internet Service":        ("utilities", "bill"),
    "Water & Sewer":           ("utilities", "bill"),
    "Auto Insurance Premium":  ("insurance", "bill"),
    "Phone Plan":              ("utilities", "bill"),
    "Streaming Bundle":        ("subscriptions", "bill"),
    "Gym Membership":          ("health", "bill"),
    "Daycare Fees":            ("daycare", "bill"),
    "Student Loan Payment":    ("loan_payment", "bill"),
    "Health Insurance":        ("insurance", "bill"),
    "Pet Food & Care":         ("pets", "bill"),
    "Commuter Pass":           ("transport", "bill"),
    "Software Subscription":   ("subscriptions", "bill"),
    "Cloud Storage":           ("subscriptions", "bill"),
    "Music Streaming":         ("subscriptions", "bill"),
    "Home Security":           ("utilities", "bill"),
    "Charity Donation":        ("shopping", "bill"),
    "Life Insurance":          ("insurance", "bill"),
    "Car Payment":             ("transport", "bill"),
    "Credit Card Minimum":     ("loan_payment", "bill"),
}

# Expected amount ranges by bill name (min,max).
BILL_AMOUNTS = {
    "Monthly Rent": (1400, 1650), "Electricity Bill": (60, 240), "Internet Service": (55, 95),
    "Water & Sewer": (40, 120), "Auto Insurance Premium": (110, 320), "Phone Plan": (45, 110),
    "Streaming Bundle": (25, 70), "Gym Membership": (30, 80), "Daycare Fees": (820, 1180),
    "Student Loan Payment": (220, 560), "Health Insurance": (180, 480), "Pet Food & Care": (35, 120),
    "Commuter Pass": (60, 160), "Software Subscription": (12, 60), "Cloud Storage": (8, 25),
    "Music Streaming": (10, 22), "Home Security": (25, 65), "Charity Donation": (20, 150),
    "Life Insurance": (55, 180), "Car Payment": (280, 620), "Credit Card Minimum": (40, 260),
}

# Fake merchants for transactions
MERCHANTS = {
    "groceries": ["Harvest Market", "Fresh Foods Co.", "Green Acres Grocer"],
    "dining": ["Bean & Leaf Coffee", "The Daily Grind", "Burger Barn", "Pizza Palace"],
    "transport": ["Metro Transit", "Fuel Stop", "City Taxi"],
    "shopping": ["MegaMart", "Online Bargains", "Fashion Hub"],
    "utilities": ["City Utilities", "PowerGrid Inc.", "AquaFlow"],
    "health": ["MediClinic", "Wellness Pharmacy"],
    "entertainment": ["Movieplex", "Arcade Zone", "Concert Hall"],
    "subscriptions": ["Streamly", "CodeForge", "SecureNet VPN"],
    "insurance": ["ShieldSafe Ins.", "ProtectAll Policy"]
}

# Standard categories (as per brief, generic)
STANDARD_CATEGORIES = [
    {"key": "income", "name": "Income", "kind": "income", "color": "#22c55e", "is_custom": 0, "hidden": 0},
    {"key": "housing", "name": "Rent/Housing", "kind": "expense", "color": "#8b5cf6", "is_custom": 0, "hidden": 0},
    {"key": "groceries", "name": "Groceries", "kind": "expense", "color": "#84cc16", "is_custom": 0, "hidden": 0},
    {"key": "dining", "name": "Dining", "kind": "expense", "color": "#f97316", "is_custom": 0, "hidden": 0},
    {"key": "transport", "name": "Transport", "kind": "expense", "color": "#06b6d4", "is_custom": 0, "hidden": 0},
    {"key": "shopping", "name": "Shopping", "kind": "expense", "color": "#3b82f6", "is_custom": 0, "hidden": 0},
    {"key": "utilities", "name": "Utilities", "kind": "expense", "color": "#eab308", "is_custom": 0, "hidden": 0},
    {"key": "health", "name": "Health", "kind": "expense", "color": "#ec4899", "is_custom": 0, "hidden": 0},
    {"key": "entertainment", "name": "Entertainment", "kind": "expense", "color": "#a855f7", "is_custom": 0, "hidden": 0},
    {"key": "subscriptions", "name": "Subscriptions", "kind": "expense", "color": "#64748b", "is_custom": 0, "hidden": 0},
    {"key": "insurance", "name": "Insurance", "kind": "expense", "color": "#14b8a6", "is_custom": 0, "hidden": 0},
    {"key": "daycare", "name": "Daycare", "kind": "expense", "color": "#0000ff", "is_custom": 1, "hidden": 0}, # Custom
    {"key": "pets", "name": "Pets", "kind": "expense", "color": "#0000ff", "is_custom": 1, "hidden": 0}, # Custom
    {"key": "loan_payment", "name": "Loan Payment", "kind": "expense", "color": "#ff4dbe", "is_custom": 1, "hidden": 0}, # Custom
    {"key": "transfer", "name": "Transfer", "kind": "transfer", "color": "#4ee4e2", "is_custom": 0, "hidden": 1},
    {"key": "savings_interest", "name": "Savings Interest", "kind": "income", "color": "#00aaff", "is_custom": 0, "hidden": 0},
    {"key": "savings_deposit", "name": "Savings Deposit", "kind": "income", "color": "#1dd743", "is_custom": 0, "hidden": 1},
    {"key": "uncategorized", "name": "Uncategorized", "kind": "expense", "color": "#525252", "is_custom": 0, "hidden": 0},
]
CATEGORY_KEYS = [c["key"] for c in STANDARD_CATEGORIES]
CATEGORY_MAP = {c["key"]: c for c in STANDARD_CATEGORIES}

# Crypto data - keeping public IDs; symbol map for correct display.
CRYPTO_MARKETS = [
    ("bitcoin", "BTC"), ("ethereum", "ETH"), ("tether", "USDT"), ("binancecoin", "BNB"),
    ("ripple", "XRP"), ("usd-coin", "USDC"), ("solana", "SOL"), ("cardano", "ADA"),
    ("dogecoin", "DOGE"), ("polkadot", "DOT"),
]
CRYPTO_MARKETS_IDS = [c[0] for c in CRYPTO_MARKETS]

# --- Helper Functions ---

def random_date_in_range(start, end):
    return start + timedelta(days=random.randint(0, (end - start).days))

def get_month_str(date_obj):
    return date_obj.strftime("%Y-%m")

def generate_monthly_amounts(base, variation=0.1):
    return [round(base * (1 + random.uniform(-variation, variation)), 2) for _ in range(MONTHS_IN_DATA)]

def get_recent_months(num_months):
    # Trailing months ending at END_DATE's month, oldest -> newest, no day-drift.
    y, m = END_DATE.year, END_DATE.month
    months = []
    for i in range(num_months - 1, -1, -1):
        mm = m - i
        yy = y
        while mm <= 0:
            mm += 12
            yy -= 1
        months.append(f"{yy:04d}-{mm:02d}")
    return months

# --- Data Generation Functions ---

def generate_accounts():
    accounts_data = []
    for i, name in enumerate(ACCOUNT_NAMES):
        kind = "checking" if "Checking" in name or "Fund" in name else "credit_card"
        bank_name = "SynthBank" if "Checking" in name or "Savings" in name else "SynthCard Issuer"
        first_date = random_date_in_range(START_DATE, START_DATE + timedelta(days=30)).strftime("%Y-%m-%d")
        last_date = random_date_in_range(END_DATE - timedelta(days=30), END_DATE).strftime("%Y-%m-%d")
        accounts_data.append({
            "id": i,
            "name": name,
            "bank": bank_name,
            "kind": kind,
            "exclude": 0,
            "cnt": random.randint(50, 500), # Placeholder, will be updated by transactions
            "first_date": first_date,
            "last_date": last_date
        })
    return accounts_data

def generate_bills(accounts_data):
    bills_data = []
    account_ids = [a["id"] for a in accounts_data if a["kind"] == "checking"]

    for i, name in enumerate(BILL_SPECS.keys()):
        category_key, kind = BILL_SPECS[name]
        lo, hi = BILL_AMOUNTS[name]
        expected_amount = round(random.uniform(lo, hi), 2)

        bills_data.append({
            "id": i,
            "name": name,
            "match_cleaned": name.lower(),
            "expected_amount": expected_amount,
            "due_day": random.randint(1, 28),
            "kind": kind,
            "account_id": random.choice(account_ids),
            "category_key": category_key,
            "active": 1,
            "created_at": random_date_in_range(START_DATE, END_DATE - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S"),
            "manual_paid_month": None,
            "payments_per_month": 1,
            "status": "due",
            "paid_date": None,
            "paid_amount": None,
            "variance": None,
            "manual": False,
            "paid_count": 0,
            "expected_count": 1,
            "remaining_count": 1,
            "per_payment": expected_amount
        })
    return bills_data

def generate_transactions(accounts_data, bills_data):
    transactions = []
    transaction_id = 0
    date_cursor = START_DATE
    
    # Store monthly aggregates for accounts and categories
    monthly_account_data = {acc["name"]: {m: {"income": 0.0, "expense": 0.0} for m in get_recent_months(MONTHS_IN_DATA)} for acc in accounts_data}
    monthly_category_data = {cat["key"]: {m: 0.0 for m in get_recent_months(MONTHS_IN_DATA)} for cat in STANDARD_CATEGORIES}

    while date_cursor <= END_DATE:
        month_str = get_month_str(date_cursor)

        # Generate income transactions (e.g., bi-weekly paychecks)
        if date_cursor.day in [5, 20]: # Twice a month
            income_amount = round(random.uniform(3100, 3600), 2)
            account = random.choice([a for a in accounts_data if a["kind"] == "checking"])
            transactions.append({
                "id": transaction_id,
                "date": date_cursor.strftime("%Y-%m-%d"),
                "description": "SynthCorp Payroll Deposit",
                "cleaned": "Payroll",
                "amount": income_amount,
                "raw_type": "CREDIT",
                "category_source": "system",
                "memo": "",
                "ckey": "income",
                "cname": "Income",
                "ccolor": CATEGORY_MAP["income"]["color"],
                "account": account["name"]
            })
            monthly_account_data[account["name"]][month_str]["income"] += income_amount
            monthly_category_data["income"][month_str] += income_amount
            transaction_id += 1

        # Generate bill payments
        for bill in bills_data:
            if bill["due_day"] == date_cursor.day:
                account = next((a for a in accounts_data if a["id"] == bill["account_id"]), None)
                if account:
                    transactions.append({
                        "id": transaction_id,
                        "date": date_cursor.strftime("%Y-%m-%d"),
                        "description": f"Payment for {bill['name']}",
                        "cleaned": bill["name"],
                        "amount": -bill["expected_amount"],
                        "raw_type": "DEBIT",
                        "category_source": "rule",
                        "memo": "",
                        "ckey": bill["category_key"],
                        "cname": CATEGORY_MAP[bill["category_key"]]["name"],
                        "ccolor": CATEGORY_MAP[bill["category_key"]]["color"],
                        "account": account["name"]
                    })
                    monthly_account_data[account["name"]][month_str]["expense"] += bill["expected_amount"]
                    monthly_category_data[bill["category_key"]][month_str] += bill["expected_amount"]
                    transaction_id += 1

        # Generate a realistic amount of everyday discretionary spend
        num_daily_expenses = random.randint(0, 3)
        for _ in range(num_daily_expenses):
            expense_category_key = random.choice([k for k in CATEGORY_KEYS if CATEGORY_MAP[k]["kind"] == "expense" and k != "uncategorized"])
            merchant = random.choice(MERCHANTS.get(expense_category_key, ["Generic Store"]))
            amount = round(random.uniform(9, 95), 2)
            account = random.choice(accounts_data)

            transactions.append({
                "id": transaction_id,
                "date": date_cursor.strftime("%Y-%m-%d"),
                "description": f"{merchant} Purchase",
                "cleaned": merchant,
                "amount": -amount,
                "raw_type": "DEBIT",
                "category_source": "manual",
                "memo": "",
                "ckey": expense_category_key,
                "cname": CATEGORY_MAP[expense_category_key]["name"],
                "ccolor": CATEGORY_MAP[expense_category_key]["color"],
                "account": account["name"]
            })
            monthly_account_data[account["name"]][month_str]["expense"] += amount
            monthly_category_data[expense_category_key][month_str] += amount
            transaction_id += 1
        
        date_cursor += timedelta(days=1)
    
    # Sort transactions by date
    transactions.sort(key=lambda x: x["date"], reverse=True)

    return transactions, monthly_account_data, monthly_category_data

def generate_crypto_markets():
    markets = []
    for rank0, (_id, symbol) in enumerate(CRYPTO_MARKETS):
        name = _id.replace("-", " ").title()
        image = f"https://coin-images.coingecko.com/coins/images/{rank0+1}/large/{_id}.png"

        price = round(random.uniform(0.1, 100000), 2)
        market_cap = int(price * random.uniform(1_000_000, 100_000_000_000))
        rank = rank0 + 1
        chg_24h = round(random.uniform(-5, 5), 5)
        chg_7d = round(random.uniform(-10, 10), 5)
        spark = [round(price * (1 + random.uniform(-0.01, 0.01)), 2) for _ in range(56)]

        markets.append({
            "id": _id,
            "symbol": symbol,
            "name": name,
            "image": image,
            "price": price,
            "market_cap": market_cap,
            "rank": rank,
            "chg_24h": chg_24h,
            "chg_7d": chg_7d,
            "spark": spark
        })
    return markets

def generate_crypto_charts(markets_data):
    charts = {}
    by_id = {m["id"]: m for m in markets_data}
    # Always chart the same coins the app may select; fall back to first 3 markets.
    chart_ids = [cid for cid in ("bitcoin", "ethereum", "solana") if cid in by_id]
    for cid in chart_ids:
        market = by_id[cid]
        chart_data = []
        base_price = market["price"] * random.uniform(0.8, 1.2)
        for i in range(169):
            date_ts = int((END_DATE - timedelta(days=168 - i)).timestamp() * 1000)
            price = round(base_price * (1 + random.uniform(-0.02, 0.02)), 2)
            chart_data.append([date_ts, price])
            base_price = price
        charts[cid] = chart_data
    return charts

def generate_crypto_holdings(markets_data):
    holdings = []
    # Take two random coins
    selected_coins = random.sample(markets_data, 2)
    for i, coin in enumerate(selected_coins):
        holdings.append({
            "id": i,
            "coin_id": coin["id"],
            "symbol": coin["symbol"],
            "name": coin["name"],
            "amount": round(random.uniform(0.001, 5), 4) # Fractional amounts
        })
    return holdings

def generate_crypto_news():
    news = []
    for i in range(3):
        news_titles = [
            "Market Volatility Expected This Quarter",
            "New Regulations Impact Crypto Industry",
            "Decentralized Finance Sees Adoption Boost",
            "Blockchain Innovation Continues Growth",
            "Major Exchange Announces New Listings"
        ]
        news.append({
            "title": random.choice(news_titles),
            "url": f"https://example.com/news/{i}",
            "source": "CryptoDaily",
            "category": "Market"
        })
    return news

def generate_savings(accounts_data, transactions):
    savings_account = next((a for a in accounts_data if a["kind"] == "checking" and "Savings" in a["name"]), None)
    if not savings_account:
        savings_account = accounts_data[1] # Default to the second account

    series = []
    deposits_total = 0.0
    withdrawals_total = 0.0
    interest_total = 0.0
    net_total = 0.0
    
    current_balance = random.uniform(5000, 15000)
    
    # Generate random savings transactions over the data period
    date_cursor = START_DATE
    while date_cursor <= END_DATE:
        if random.random() < 0.3: # ~30% chance of a deposit
            amount = round(random.uniform(100, 1000), 2)
            series.append({
                "date": date_cursor.strftime("%Y-%m-%d"),
                "description": "SynthCorp Savings Deposit",
                "amount": amount,
                "cat_key": "savings_deposit"
            })
            deposits_total += amount
            current_balance += amount
        
        if random.random() < 0.15: # ~15% chance of a withdrawal
            amount = round(random.uniform(50, 500), 2)
            if current_balance - amount > 0: # Ensure balance doesn't go negative
                series.append({
                    "date": date_cursor.strftime("%Y-%m-%d"),
                    "description": "Transfer to Checking",
                    "amount": -amount,
                    "cat_key": "transfer"
                })
                withdrawals_total += amount
                current_balance -= amount

        if date_cursor.day == 28: # Monthly interest
            interest = round(current_balance * random.uniform(0.001, 0.003), 2)
            series.append({
                "date": date_cursor.strftime("%Y-%m-%d"),
                "description": "Monthly Interest Payment",
                "amount": interest,
                "cat_key": "savings_interest"
            })
            interest_total += interest
            current_balance += interest

        date_cursor += timedelta(days=random.randint(1, 5)) # Advance a few days

    net_total = deposits_total - withdrawals_total + interest_total
    
    savings_accounts = [{
        "id": savings_account["id"],
        "name": savings_account["name"],
        "bank": savings_account["bank"],
        "deposits": round(deposits_total, 2),
        "withdrawals": round(withdrawals_total, 2),
        "interest": round(interest_total, 2),
        "net": round(net_total, 2),
        "count": len(series),
        "last_date": series[-1]["date"] if series else None,
        "series": series,
        "balance": round(current_balance, 2),
        "balance_date": END_DATE.strftime("%Y-%m-%d")
    }]
    
    savings_totals = {
        "deposits": round(deposits_total, 2),
        "withdrawals": round(withdrawals_total, 2),
        "interest": round(interest_total, 2),
        "net": round(net_total, 2),
        "count": len(series),
        "balance": round(current_balance, 2)
    }
    
    return {"accounts": savings_accounts, "totals": savings_totals}

def generate_savings_history(savings_data):
    months_history = []
    # Simulate a year of history, starting from END_DATE backwards
    current_date = END_DATE
    balance = savings_data["totals"]["balance"]
    
    for i in range(12):
        month_str = get_month_str(current_date)
        # Simple random walk for past balances
        prev_balance = balance + random.uniform(-500, 500)
        months_history.append({"month": month_str, "balance": round(prev_balance, 2)})
        balance = prev_balance
        
        current_date -= timedelta(days=30)
    
    months_history.reverse() # Oldest to newest
    
    current_month_str = get_month_str(END_DATE)
    
    return {
        "months": months_history,
        "current": savings_data["totals"]["balance"],
        "current_month": current_month_str,
        "txn_net": savings_data["totals"]["net"] # This needs to be calculated for the current month only
    }

def generate_paydown(accounts_data):
    cards = []
    credit_cards = [a for a in accounts_data if a["kind"] == "credit_card"]
    
    for i, card in enumerate(credit_cards[:3]): # Max 3 cards in schema
        balance = round(random.uniform(500, 5000), 2)
        apr = round(random.uniform(15.0, 29.9), 2)
        min_payment = round(balance * random.uniform(0.02, 0.05), 2)
        payments_count = random.randint(5, 20)
        payments_total = round(min_payment * payments_count * random.uniform(0.8, 1.2), 2)
        charges_total = round(payments_total + balance * random.uniform(0.1, 0.3), 2)
        
        cards.append({
            "id": card["id"],
            "name": card["name"],
            "bank": card["bank"],
            "balance": balance,
            "balance_date": END_DATE.strftime("%Y-%m-%d"),
            "apr": apr,
            "min_payment": min_payment,
            "payments_count": payments_count,
            "payments_total": payments_total,
            "payments_last": random_date_in_range(END_DATE - timedelta(days=30), END_DATE).strftime("%Y-%m-%d"),
            "charges_total": charges_total
        })
    
    total_owed = sum(c["balance"] for c in cards)
    total_min_payment = sum(c["min_payment"] for c in cards)
    avg_apr = sum(c["apr"] for c in cards) / len(cards) if cards else 0
    
    return {
        "cards": cards,
        "totals": {
            "owed": round(total_owed, 2),
            "min": round(total_min_payment, 2),
            "avg_apr": round(avg_apr, 2),
            "cards_with_balance": len(cards),
            "cards_total": len(credit_cards)
        }
    }

def generate_ai_costs():
    total_cost = round(random.uniform(50, 500), 2)
    count = random.randint(100, 1000)
    first_date = random_date_in_range(START_DATE, START_DATE + timedelta(days=60)).strftime("%Y-%m-%d")
    last_date = random_date_in_range(END_DATE - timedelta(days=30), END_DATE).strftime("%Y-%m-%d")

    daily_data = []
    daily_by_model_data = []
    monthly_data = []
    by_model_data = []
    by_wallet_data = []
    models_list = ["Gemini-Pro", "GPT-4o"]
    
    date_cursor = START_DATE
    while date_cursor <= END_DATE:
        cost = round(random.uniform(0.5, 5), 2)
        n = random.randint(10, 50)
        daily_data.append({"date": date_cursor.strftime("%Y-%m-%d"), "cost": cost, "n": n})
        
        # Split daily cost by model
        for model in models_list:
            model_cost = round(cost * random.uniform(0.3, 0.7), 2)
            model_n = random.randint(n // 2, n)
            daily_by_model_data.append({"date": date_cursor.strftime("%Y-%m-%d"), "cost": model_cost, "n": model_n, "model": model})
        
        date_cursor += timedelta(days=random.randint(1, 3)) # Simulate some days without data for brevity

    # Aggregate monthly
    monthly_temp = {}
    for d in daily_data:
        month_str = get_month_str(datetime.strptime(d["date"], "%Y-%m-%d"))
        monthly_temp.setdefault(month_str, {"cost": 0, "n": 0})
        monthly_temp[month_str]["cost"] += d["cost"]
        monthly_temp[month_str]["n"] += d["n"]
    for month, data in monthly_temp.items():
        monthly_data.append({"month": month, "cost": round(data["cost"], 2), "n": data["n"]})

    # Aggregate by model
    by_model_temp = {}
    for d in daily_by_model_data:
        by_model_temp.setdefault(d["model"], {"cost": 0, "n": 0})
        by_model_temp[d["model"]]["cost"] += d["cost"]
        by_model_temp[d["model"]]["n"] += d["n"]
    for model, data in by_model_temp.items():
        by_model_data.append({"model": model, "cost": round(data["cost"], 2), "n": data["n"]})
    
    # By wallet (simple, one wallet for now)
    by_wallet_data.append({"wallet_type": "Main Wallet", "cost": total_cost, "n": count})

    return {
        "total": round(total_cost, 2),
        "count": count,
        "first_date": first_date,
        "last_date": last_date,
        "daily": daily_data,
        "daily_by_model": daily_by_model_data,
        "monthly": monthly_data,
        "by_model": by_model_data,
        "by_wallet": by_wallet_data,
        "models": models_list
    }

def generate_economy_news():
    news_headlines = [
        "Inflation Concerns Persist as Data Released",
        "Central Bank Hints at Rate Changes",
        "Global Markets React to Geopolitical Events",
        "Tech Sector Shows Mixed Performance",
        "Consumer Spending Holds Steady in Q3",
        "Energy Prices Fluctuate Amid Supply Worries",
        "Housing Market Activity Slows Down",
        "Manufacturing Output Beats Expectations"
    ]
    
    economy_news = []
    bonds_news = []
    
    for i in range(8):
        pub_date = random_date_in_range(END_DATE - timedelta(days=60), END_DATE).strftime("%Y-%m-%d")
        economy_news.append({
            "title": random.choice(news_headlines),
            "url": f"https://economy.example.com/article/{i}",
            "source": "EconUpdate",
            "published": pub_date
        })
    
    for i in range(4):
        pub_date = random_date_in_range(END_DATE - timedelta(days=60), END_DATE).strftime("%Y-%m-%d")
        bonds_news.append({
            "title": f"Bond Yields Shift on Market Sentiment {i}",
            "url": f"https://bonds.example.com/article/{i}",
            "source": "BondInsights",
            "published": pub_date
        })

    return {"economy": economy_news, "bonds": bonds_news}

def generate_economy_markets():
    return {
        "rates": {
            "10y": {"label": "10-Year Treasury Yield", "value": round(random.uniform(3.5, 4.5), 2)},
            "5y": {"label": "5-Year Treasury Yield", "value": round(random.uniform(3.0, 4.0), 2)},
            "2y": {"label": "2-Year Treasury Yield", "value": round(random.uniform(2.5, 3.5), 2)},
            "13w": {"label": "13-Week Bill Yield", "value": round(random.uniform(2.0, 3.0), 2)},
            "10y2y": {"label": "10y-2y Spread", "value": round(random.uniform(-0.5, 1.0), 2)}
        }
    }


def generate_full_data():
    data = {}
    
    accounts_data = generate_accounts()
    bills_data = generate_bills(accounts_data)
    transactions, monthly_account_data, monthly_category_data = generate_transactions(accounts_data, bills_data)

    # --- Update derived data based on transactions ---
    
    # Update account counts based on actual transactions
    account_transaction_counts = {acc["name"]: 0 for acc in accounts_data}
    for t in transactions:
        account_transaction_counts[t["account"]] += 1
    for acc in accounts_data:
        acc["cnt"] = account_transaction_counts.get(acc["name"], 0)

    # Dashboard - Months and Categories
    dashboard_months = []
    dashboard_categories = []
    
    total_income_all_months = 0.0
    total_expense_all_months = 0.0

    recent_months_list = get_recent_months(TREND_MONTHS)
    
    for month_str in recent_months_list:
        income_this_month = sum(monthly_category_data["income"].get(month_str, 0) for _ in range(1)) # sum is redundant for single value but for clarity
        expense_this_month = sum(monthly_category_data[cat_key].get(month_str, 0) for cat_key in CATEGORY_KEYS if CATEGORY_MAP[cat_key]["kind"] == "expense")
        net_this_month = income_this_month - expense_this_month
        dashboard_months.append({
            "month": month_str,
            "income": round(income_this_month, 2),
            "expense": round(expense_this_month, 2),
            "net": round(net_this_month, 2)
        })
        total_income_all_months += income_this_month
        total_expense_all_months += expense_this_month

    for category in STANDARD_CATEGORIES:
        if category["kind"] == "transfer": continue # Exclude transfer categories from dashboard categories
        
        cat_key = category["key"]
        
        total_cat_amount = 0.0
        series_data = {}
        for month_str in recent_months_list:
            amount = monthly_category_data[cat_key].get(month_str, 0.0)
            total_cat_amount += amount
            series_data[month_str] = round(amount, 2)
        
        count = sum(1 for t in transactions if t["ckey"] == cat_key)
        
        # Calculate avg_3mo (last 3 months)
        avg_3mo_sum = sum(monthly_category_data[cat_key].get(m, 0) for m in recent_months_list[-3:])
        avg_3mo = round(avg_3mo_sum / 3, 2) if avg_3mo_sum else 0.0

        dashboard_categories.append({
            "key": cat_key,
            "name": category["name"],
            "color": category["color"],
            "kind": category["kind"],
            "total": round(total_cat_amount, 2),
            "count": count,
            "hidden": category["hidden"],
            "avg_3mo": avg_3mo,
            "avg_active": avg_3mo, # Simplified for demo
            "series": series_data
        })
    
    # Dashboard - Accounts income/expense
    dashboard_accounts_summary = []
    for acc in accounts_data:
        total_income = sum(monthly_account_data[acc["name"]][m]["income"] for m in recent_months_list)
        total_expense = sum(monthly_account_data[acc["name"]][m]["expense"] for m in recent_months_list)
        dashboard_accounts_summary.append({
            "name": acc["name"],
            "income": round(total_income, 2),
            "expense": round(total_expense, 2)
        })

    dashboard_totals = {
        "income": round(total_income_all_months, 2),
        "expense": round(total_expense_all_months, 2),
        "net": round(total_income_all_months - total_expense_all_months, 2),
        "savings": 0.0 # Not directly calculated here, use savings data
    }
    
    # Simplified grade calculation
    grade_net = dashboard_totals["net"]
    grade = "C"
    color = "#f59e0b"
    rate = 10.0
    if grade_net > 5000:
        grade = "A"
        color = "#22c55e"
        rate = 25.0
    elif grade_net > 1000:
        grade = "B"
        color = "#84cc16"
        rate = 15.0

    data["dashboard"] = {
        "totals": dashboard_totals,
        "grade": {
            "grade": grade,
            "color": color,
            "rate": rate,
            "net": dashboard_totals["net"],
            "income": dashboard_totals["income"],
            "expense": dashboard_totals["expense"],
            "trend": {
                "direction": random.choice(["up", "down", "flat"]),
                "delta": round(random.uniform(0.5, 5.0), 2),
                "prev_grade": random.choice(["A", "B", "C", "D"]),
                "prev_rate": round(random.uniform(5.0, 30.0), 2)
            },
            "all_time": {
                "grade": grade, # Simplified
                "color": color, # Simplified
                "rate": rate, # Simplified
                "net": dashboard_totals["net"] * 2, # Example for all-time
                "income": dashboard_totals["income"] * 2,
                "expense": dashboard_totals["expense"] * 2,
                "savings": 0.0
            },
            "savings": 0.0
        },
        "categories": dashboard_categories,
        "months": dashboard_months,
        "accounts": dashboard_accounts_summary,
        "uncategorized": {"count": 0, "amount": 0},
        "range": {"start": START_DATE.strftime("%Y-%m-%d"), "end": END_DATE.strftime("%Y-%m-%d")}
    }

    # Budgets
    budget_month = get_month_str(END_DATE)
    budget_rows = []
    budget_categories = []
    total_budget = 0.0
    total_spent_budgeted = 0.0
    total_spent_all = 0.0
    
    for cat in STANDARD_CATEGORIES:
        if cat["kind"] == "transfer": continue
        budget_categories.append({"key": cat["key"], "name": cat["name"], "color": cat["color"]})
        
        budget_amount = 0.0
        if cat["key"] in ["groceries", "dining", "housing", "transport"]:
            budget_amount = round(random.uniform(200, 2000), 2)
        elif cat["key"] == "income": # Income is not budgeted as an expense
             pass
        else:
            budget_amount = round(random.uniform(50, 500), 2)
        
        spent_amount = sum(t["amount"] for t in transactions if t["ckey"] == cat["key"] and get_month_str(datetime.strptime(t["date"], "%Y-%m-%d")) == budget_month)
        spent_amount = abs(spent_amount) # Budgets display as positive spent

        total_spent_all += spent_amount
        
        remaining = budget_amount - spent_amount
        pct = round((spent_amount / budget_amount) * 100, 1) if budget_amount > 0 else (100 if spent_amount > 0 else None)
        
        if budget_amount > 0:
            total_budget += budget_amount
            total_spent_budgeted += spent_amount

        budget_rows.append({
            "key": cat["key"],
            "name": cat["name"],
            "color": cat["color"],
            "budget": round(budget_amount, 2),
            "spent": round(spent_amount, 2),
            "remaining": round(remaining, 2),
            "pct": pct
        })

    # Add crypto to budgets as well
    for market in generate_crypto_markets()[:1]: # Just bitcoin for budget example
        spent_crypto = round(random.uniform(100, 1000), 2)
        budget_crypto = round(random.uniform(500, 1500), 2)
        remaining_crypto = budget_crypto - spent_crypto
        pct_crypto = round((spent_crypto / budget_crypto) * 100, 1) if budget_crypto > 0 else None
        
        budget_rows.append({
            "key": market["id"],
            "name": market["name"],
            "color": "#ff9900", # Specific color for crypto
            "budget": budget_crypto,
            "spent": spent_crypto,
            "remaining": remaining_crypto,
            "pct": pct_crypto
        })
        budget_categories.append({"key": market["id"], "name": market["name"], "color": "#ff9900"})
        total_budget += budget_crypto
        total_spent_budgeted += spent_crypto
        total_spent_all += spent_crypto # Include crypto in total_spent_all

    data["budgets"] = {
        "month": budget_month,
        "rows": budget_rows,
        "categories": budget_categories,
        "totals": {
            "budget": round(total_budget, 2),
            "spent": round(total_spent_budgeted, 2),
            "spent_all": round(total_spent_all, 2),
            "spent_unbudgeted": round(total_spent_all - total_spent_budgeted, 2)
        }
    }

    # Trends
    trend_months = recent_months_list # Same as dashboard for simplicity
    trend_categories = []
    
    for category in STANDARD_CATEGORIES:
        if category["kind"] == "transfer": continue
        cat_key = category["key"]
        
        total_cat_amount = 0.0
        series_data = {}
        for month_str in trend_months:
            amount = monthly_category_data[cat_key].get(month_str, 0.0)
            total_cat_amount += amount
            series_data[month_str] = round(amount, 2)
        
        avg_3mo_sum = sum(monthly_category_data[cat_key].get(m, 0) for m in trend_months[-3:])
        avg_3mo = round(avg_3mo_sum / 3, 2) if avg_3mo_sum else 0.0

        trend_categories.append({
            "key": cat_key,
            "name": category["name"],
            "color": category["color"],
            "total": round(total_cat_amount, 2),
            "avg_3mo": avg_3mo,
            "avg_active": avg_3mo,
            "series": series_data
        })
    
    trend_month_series = dashboard_months # Re-use for simplicity
    
    avg_3mo_income = sum(m["income"] for m in dashboard_months[-3:]) / 3
    avg_3mo_expense = sum(m["expense"] for m in dashboard_months[-3:]) / 3
    avg_3mo_net = sum(m["net"] for m in dashboard_months[-3:]) / 3

    avg_all_income = sum(m["income"] for m in dashboard_months) / len(dashboard_months)
    avg_all_expense = sum(m["expense"] for m in dashboard_months) / len(dashboard_months)
    avg_all_net = sum(m["net"] for m in dashboard_months) / len(dashboard_months)

    data["trends"] = {
        "months": trend_months,
        "categories": trend_categories,
        "month_series": trend_month_series,
        "avg_3mo": {
            "income": round(avg_3mo_income, 2),
            "expense": round(avg_3mo_expense, 2),
            "net": round(avg_3mo_net, 2)
        },
        "avg_all": {
            "income": round(avg_all_income, 2),
            "expense": round(avg_all_expense, 2),
            "net": round(avg_all_net, 2),
            "months": len(dashboard_months)
        }
    }

    data["categories"] = [{**c, "id": i} for i, c in enumerate(STANDARD_CATEGORIES)]
    data["accounts"] = accounts_data
    data["transactions"] = {"total": len(transactions), "items": transactions}
    
    # Rules - simple merchant patterns based on generated merchants
    rules_data = []
    rule_id = 0
    for ckey, merchants_list in MERCHANTS.items():
        if ckey == "groceries": # Example with multiple patterns
            rules_data.append({"id": rule_id, "merchant_pattern": random.choice(merchants_list), "category_key": ckey, "hits": random.randint(5, 50)})
            rule_id += 1
            rules_data.append({"id": rule_id, "merchant_pattern": random.choice(merchants_list), "category_key": ckey, "hits": random.randint(5, 50)})
            rule_id += 1
        else:
            rules_data.append({"id": rule_id, "merchant_pattern": random.choice(merchants_list), "category_key": ckey, "hits": random.randint(5, 50)})
            rule_id += 1

    # Add rules for bill names
    for bill in bills_data:
        rules_data.append({
            "id": rule_id,
            "merchant_pattern": bill["name"],
            "category_key": bill["category_key"],
            "hits": random.randint(5, 20)
        })
        rule_id += 1

    data["rules"] = rules_data

    # Imports (single entry)
    data["imports"] = [{
        "id": "demo-synth-1",
        "filename": "synthetic_statement.csv",
        "account": accounts_data[0]["name"],
        "bank": accounts_data[0]["bank"],
        "inserted": len(transactions),
        "skipped": 0,
        "created_at": END_DATE.strftime("%Y-%m-%d %H:%M:%S")
    }]

    data["savings"] = generate_savings(accounts_data, transactions)
    data["crypto"] = {
        "markets": generate_crypto_markets(),
        "charts": generate_crypto_charts(generate_crypto_markets()),
        "favorites": ["bitcoin", "ethereum"],
        "holdings": generate_crypto_holdings(generate_crypto_markets()),
        "news": generate_crypto_news()
    }
    data["savingsHistory"] = generate_savings_history(data["savings"])

    # Bills — build a realistic current-month snapshot (month = END_DATE's month).
    cur_month = get_month_str(END_DATE)
    DEMO_DAY = 12   # "today" within the demo month

    def _date_in_month(month_str, day):
        yy, mm = month_str.split('-')
        return f"{yy}-{mm}-{min(day,28):02d}"

    for bill in bills_data:
        bill["expected_count"] = 1
        bill["paid_count"] = 0
        bill["remaining_count"] = 1
        bill["per_payment"] = bill["expected_amount"]
        bill["paid_date"] = None
        bill["paid_amount"] = None
        bill["variance"] = None

        if bill["due_day"] <= DEMO_DAY:
            # already due this month -> paid
            bill["status"] = "paid"
            bill["paid_date"] = _date_in_month(cur_month, bill["due_day"])
            bill["paid_amount"] = bill["expected_amount"]
            bill["paid_count"] = 1
            bill["remaining_count"] = 0
        elif bill["due_day"] <= DEMO_DAY + 6:
            # due next few days -> one is partial (multi-payment style), rest due
            if bill["id"] % 4 == 0:
                half = round(bill["expected_amount"] / 2, 2)
                bill["status"] = "partial"
                bill["payments_per_month"] = 2
                bill["expected_count"] = 2
                bill["paid_count"] = 1
                bill["remaining_count"] = 1
                bill["per_payment"] = half
                bill["paid_date"] = _date_in_month(cur_month, max(1, bill["due_day"] - 3))
                bill["paid_amount"] = half
            else:
                bill["status"] = "due"
        else:
            bill["status"] = "due"

    bill_counts = {
        "paid": sum(1 for b in bills_data if b["status"] == "paid"),
        "partial": sum(1 for b in bills_data if b["status"] == "partial"),
        "due": sum(1 for b in bills_data if b["status"] == "due"),
        "overdue": sum(1 for b in bills_data if b["status"] == "overdue"),
        "total": len(bills_data),
        "expected": round(sum(b["expected_amount"] for b in bills_data), 2),
        "paid_amount": round(sum(b["paid_amount"] or 0 for b in bills_data), 2),
        "left": round(sum((b["expected_amount"] - (b["paid_amount"] or 0)) for b in bills_data), 2)
    }

    data["bills"] = {"month": cur_month, "rows": bills_data, "counts": bill_counts}
    data["paydown"] = generate_paydown(accounts_data)
    data["aiCosts"] = generate_ai_costs()
    data["economyNews"] = generate_economy_news()
    data["economyMarkets"] = generate_economy_markets()
    
    return data

if __name__ == "__main__":
    synthetic_data = generate_full_data()
    output_path = "/Users/jaime/.openclaw/workspace/finance-tracker/docs/demo.json"
    with open(output_path, "w") as f:
        json.dump(synthetic_data, f, indent=2)
    print(f"Successfully generated synthetic data to {output_path}")

