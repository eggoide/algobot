#!/bin/sh
# AlgoBot heartbeat watchdog. Reads mtime of /reports/status.json — if the bot
# stops updating it for longer than HEARTBEAT_MAX_AGE_SEC, sends a Telegram alert
# (rate-limited so the channel isn't spammed).
set -eu

STATUS_FILE="${STATUS_FILE:-/reports/status.json}"
MAX_AGE="${HEARTBEAT_MAX_AGE_SEC:-600}"
INTERVAL="${HEARTBEAT_INTERVAL:-60}"
ALERT_COOLDOWN="${HEARTBEAT_ALERT_COOLDOWN_SEC:-1800}"

apk add --no-cache curl >/dev/null 2>&1 || true

last_alert=0

send_tg() {
  if [ -z "${TG_TOKEN:-}" ] || [ -z "${TG_CHAT_ID:-}" ]; then
    echo "[heartbeat] TG creds missing — skipping alert"
    return 0
  fi
  curl -fsS --max-time 5 \
    -G "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TG_CHAT_ID}" \
    --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

echo "[heartbeat] watching $STATUS_FILE; max_age=${MAX_AGE}s; interval=${INTERVAL}s"

while true; do
  now=$(date +%s)
  if [ ! -f "$STATUS_FILE" ]; then
    age=999999
  else
    mtime=$(stat -c %Y "$STATUS_FILE" 2>/dev/null || echo 0)
    age=$((now - mtime))
  fi

  if [ "$age" -gt "$MAX_AGE" ]; then
    if [ $((now - last_alert)) -gt "$ALERT_COOLDOWN" ]; then
      msg="WARN: AlgoBot heartbeat — status.json stale (${age}s > ${MAX_AGE}s). Bot may be frozen."
      echo "[heartbeat] $msg"
      send_tg "$msg"
      last_alert=$now
    fi
  fi

  sleep "$INTERVAL"
done
