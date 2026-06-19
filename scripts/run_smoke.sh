#!/bin/bash
# Run the authenticated PJUD smoke and ALERT on failure.
#
# IMPORTANT: run with the freshness scraper PAUSED — the smoke is headful and
# PJUD allows one authenticated browser/session at a time. Intended to run in a
# short maintenance window or on a dedicated box, NOT concurrently with the
# continuous freshness worker.
#
# Alert delivery (optional, first configured channel wins):
#   - PJUD_ALERT_WEBHOOK_URL  → POSTs a JSON {"text": "..."} (Slack/Discord/etc.)
#   - else: logs "no channel configured" (wire SendGrid/webhook to enable email).
#
# Exit code mirrors the smoke (0 = all checks passed).

cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
set -a; source .env.backfill; set +a
export PYTHONPATH="$(pwd)"

VENV=$(ls -d "$HOME"/Library/Caches/pypoetry/virtualenvs/segal-case-tracker-*-py3.11/bin/python 2>/dev/null | head -1)
TS=$(date '+%F %T')

OUT=$("$VENV" scripts/smoke_pjud.py 2>/dev/null | tail -1)
CODE=$?
echo "[$TS] smoke exit=$CODE $OUT"

if [ "$CODE" -ne 0 ]; then
  MSG="🔴 PJUD smoke FAILED (exit $CODE) at $TS — $OUT"
  if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    curl -s -m 12 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$MSG" >/dev/null \
      && echo "alert: telegram sent" || echo "alert: telegram failed"
  elif [ -n "$PJUD_ALERT_WEBHOOK_URL" ]; then
    curl -s -m 10 -X POST -H 'Content-Type: application/json' \
      --data "$(printf '{"text":%s}' "$(printf '%s' "$MSG" | "$VENV" -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")" \
      "$PJUD_ALERT_WEBHOOK_URL" >/dev/null && echo "alert: webhook sent" || echo "alert: webhook send failed"
  else
    echo "alert: NO channel configured — set TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID (or PJUD_ALERT_WEBHOOK_URL). Failure artifacts in ${SMOKE_FAILURE_DIR:-/tmp}/smoke_fail_*"
  fi
fi

exit "$CODE"
