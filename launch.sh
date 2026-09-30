#!/bin/bash
# Auto-launch Chombi Finance Tracker (checks if already running)
cd /Users/jaime/.openclaw/workspace/finance-tracker

if pgrep -f "uvicorn app.main:app" > /dev/null; then
    echo "$(date): Finance tracker already running"
    exit 0
fi

exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8770 >> server.log 2>&1
