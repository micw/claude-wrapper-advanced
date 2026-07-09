#!/usr/bin/env bash
# Startet den Proxy lokal. .env wird geladen, falls vorhanden.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

exec uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" "$@"
