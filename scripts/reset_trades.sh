#!/usr/bin/env bash
# Kompletní reset obchodní historie a stavu bota.
# Před spuštěním prosím zavři případné otevřené pozice na IB paper účtu,
# jinak je reconcile znovu objeví po startu bota.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${PROJECT_DIR}/volumes/data"
REPORTS_DIR="${PROJECT_DIR}/volumes/reports"
BACKUP_DIR="${PROJECT_DIR}/volumes/backups/reset-$(date +%Y%m%d-%H%M%S)"

# --- flagy ---
ASSUME_YES=0
SKIP_RESTART=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes)         ASSUME_YES=1 ;;
        --no-restart)     SKIP_RESTART=1 ;;
        -h|--help)
            cat <<EOF
Použití: $(basename "$0") [--yes] [--no-restart]

  --yes, -y       Bez interaktivního potvrzení.
  --no-restart    Nespouštět bota po vyčištění (default = docker compose start bot).

Skript:
  1) zastaví kontejner 'bot'
  2) zazálohuje DB + state do volumes/backups/reset-<timestamp>/
  3) smaže algobot.db (+ WAL/SHM), bot_state.json a dashboard JSONy
  4) volitelně restartuje kontejner 'bot'
EOF
            exit 0
            ;;
        *)
            echo "Neznámý argument: $arg (viz --help)" >&2
            exit 1
            ;;
    esac
done

echo "Projekt:  $PROJECT_DIR"
echo "Data:     $DATA_DIR"
echo "Reports:  $REPORTS_DIR"
echo "Backup →  $BACKUP_DIR"
echo

if [[ "$ASSUME_YES" -ne 1 ]]; then
    read -r -p "Opravdu vymazat kompletní historii a stav bota? [ano/NE]: " reply
    if [[ "$reply" != "ano" ]]; then
        echo "Zrušeno."
        exit 1
    fi
fi

# --- 1) zastav bota ---
echo "[1/4] Zastavuji kontejner 'bot'…"
( cd "$PROJECT_DIR" && docker compose stop bot ) || true

# --- 2) záloha ---
echo "[2/4] Zálohuji do $BACKUP_DIR"
sudo mkdir -p "$BACKUP_DIR"
shopt -s nullglob
for f in "$DATA_DIR"/algobot.db "$DATA_DIR"/algobot.db-wal "$DATA_DIR"/algobot.db-shm \
         "$DATA_DIR"/bot_state.json "$DATA_DIR"/trade_history.csv; do
    [[ -e "$f" ]] && sudo cp -a "$f" "$BACKUP_DIR/"
done
shopt -u nullglob
sudo chown -R "$(id -u):$(id -g)" "$BACKUP_DIR"

# --- 3) smazání ---
echo "[3/4] Mažu DB, state a dashboard JSONy…"
sudo rm -f \
    "$DATA_DIR/algobot.db" \
    "$DATA_DIR/algobot.db-wal" \
    "$DATA_DIR/algobot.db-shm" \
    "$DATA_DIR/bot_state.json"

# staré .bak-* z minulých čištění – necháváme být (jsou to už zálohy),
# ale trade_history.csv (legacy) čistíme:
sudo rm -f "$DATA_DIR/trade_history.csv"

# dashboard cache – bot si je stejně přepíše, ale ať po restartu není zmatek
sudo rm -f \
    "$REPORTS_DIR/trades.json" \
    "$REPORTS_DIR/portfolio.json" \
    "$REPORTS_DIR/equity_curve.json" \
    "$REPORTS_DIR/strategy_state.json" \
    "$REPORTS_DIR/candidates.json" \
    "$REPORTS_DIR/status.json"

# --- 4) start ---
if [[ "$SKIP_RESTART" -eq 1 ]]; then
    echo "[4/4] --no-restart → bota nespouštím."
else
    echo "[4/4] Startuji kontejner 'bot'…"
    ( cd "$PROJECT_DIR" && docker compose start bot )
fi

echo
echo "Hotovo. Záloha: $BACKUP_DIR"
echo "Ověř logy:  docker compose logs -f bot"
