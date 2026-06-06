#!/bin/sh
# AlgoBot heartbeat watchdog. Reads mtime of /reports/status.json — if the bot
# stops updating it for longer than HEARTBEAT_MAX_AGE_SEC, sends a Telegram alert
# (rate-limited so the channel isn't spammed).
set -eu

STATUS_FILE="${STATUS_FILE:-/reports/status.json}"
MAX_AGE="${HEARTBEAT_MAX_AGE_SEC:-600}"
INTERVAL="${HEARTBEAT_INTERVAL:-60}"
ALERT_COOLDOWN="${HEARTBEAT_ALERT_COOLDOWN_SEC:-1800}"
# NYSE alert window (minutes since NY midnight). Default 09:45 NY (585) to 16:05
# NY (965): 15 min grace after open (bot's first cycle runs ~09:35) and 5 min
# after close. Outside the window the bot is legitimately sleeping, so we
# suppress alerts to avoid weekend/overnight spam.
WIN_START="${HEARTBEAT_WINDOW_START_MIN:-585}"
WIN_END="${HEARTBEAT_WINDOW_END_MIN:-965}"

apk add --no-cache curl tzdata >/dev/null 2>&1 || true

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

# 0 (true) when NYSE is in trading hours and we expect bot writes; 1 otherwise.
# Doesn't handle US holidays — we tolerate a couple of false alerts on those.
in_alert_window() {
  dow=$(TZ=America/New_York date +%u)
  case "$dow" in
    6|7) return 1 ;;
  esac
  hh=$(TZ=America/New_York date +%H)
  mm=$(TZ=America/New_York date +%M)
  # 1$hh trick avoids POSIX-sh octal pitfall on 08/09 (e.g. 108-100=8).
  total_min=$(( (1$hh - 100) * 60 + (1$mm - 100) ))
  if [ "$total_min" -lt "$WIN_START" ] || [ "$total_min" -ge "$WIN_END" ]; then
    return 1
  fi
  return 0
}

echo "[heartbeat] watching $STATUS_FILE; max_age=${MAX_AGE}s; interval=${INTERVAL}s; window=NY ${WIN_START}-${WIN_END} min, Mon-Fri"

while true; do
  now=$(date +%s)
  if [ ! -f "$STATUS_FILE" ]; then
    age=999999
  else
    mtime=$(stat -c %Y "$STATUS_FILE" 2>/dev/null || echo 0)
    age=$((now - mtime))
  fi

  if [ "$age" -gt "$MAX_AGE" ] && in_alert_window; then
    if [ $((now - last_alert)) -gt "$ALERT_COOLDOWN" ]; then
      msg="WARN: AlgoBot heartbeat — status.json stale (${age}s > ${MAX_AGE}s). Bot may be frozen."
      echo "[heartbeat] $msg"
      send_tg "$msg"
      last_alert=$now
    fi
  fi

  sleep "$INTERVAL"
done
