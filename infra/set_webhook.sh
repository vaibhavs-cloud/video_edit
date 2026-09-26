#!/usr/bin/env bash
# One-time Telegram webhook setup. Run after the Worker is deployed:
#   TELEGRAM_BOT_TOKEN=... TELEGRAM_SECRET_TOKEN=... TELEGRAM_SECRET_PATH=... \
#   WORKER_URL=https://vedit-relay.<subdomain>.workers.dev ./infra/set_webhook.sh
set -euo pipefail

: "${TELEGRAM_BOT_TOKEN:?set TELEGRAM_BOT_TOKEN}"
: "${TELEGRAM_SECRET_TOKEN:?set TELEGRAM_SECRET_TOKEN}"
: "${TELEGRAM_SECRET_PATH:?set TELEGRAM_SECRET_PATH}"
: "${WORKER_URL:?set WORKER_URL, e.g. https://vedit-relay.example.workers.dev}"

URL="${WORKER_URL%/}/tg/${TELEGRAM_SECRET_PATH}"

curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg url "$URL" --arg s "$TELEGRAM_SECRET_TOKEN" \
        '{url: $url, secret_token: $s, drop_pending_updates: true}')" | jq .

echo "webhook set to ${URL}"
