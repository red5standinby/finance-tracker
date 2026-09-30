#!/usr/bin/env bash
# Chombi Finance Tracker launcher
cd "$(dirname "$0")"
PORT="${PORT:-8770}"
HOST="${HOST:-0.0.0.0}"
exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
