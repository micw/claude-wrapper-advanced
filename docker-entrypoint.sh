#!/usr/bin/env bash
# Startet den Proxy. Prüft den CLI-Login und gibt bei fehlender Auth einen deutlichen
# Hinweis aus — bricht aber NICHT ab, damit man in den laufenden Container gehen und
# sich einloggen kann (der Login persistiert über das CLAUDE_CONFIG_DIR-Volume).
set -euo pipefail

status="$(claude auth status --json 2>/dev/null || true)"
if printf '%s' "$status" | grep -q '"loggedIn": *true'; then
  # email/subscriptionType fehlen bei Token-Auth (CLAUDE_CODE_OAUTH_TOKEN). '|| true', damit ein
  # leeres grep unter 'set -euo pipefail' den Entrypoint NICHT vor uvicorn abbrechen lässt.
  email="$(printf '%s' "$status" | grep -oE '"email": *"[^"]*"' | cut -d'"' -f4 || true)"
  plan="$(printf '%s' "$status" | grep -oE '"subscriptionType": *"[^"]*"' | cut -d'"' -f4 || true)"
  echo "[entrypoint] Claude CLI authenticated (${email:-token}, plan=${plan:-?})."
else
  cat <<'EOF'
========================================================================
[entrypoint] Claude CLI is NOT authenticated.
The server will start anyway; /v1/* requests return 503 until you log in.

Log in without leaving the container running one of:

  docker compose exec proxy claude /login
  docker compose exec proxy claude setup-token      # prints a long-lived token

The credentials live in CLAUDE_CONFIG_DIR (mounted volume) and persist
across restarts. No token needs to be set in .env for the /login path.
========================================================================
EOF
fi

exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
