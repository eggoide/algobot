import datetime
import json
import os
import sys
import time
from collections import deque
from io import StringIO
from typing import Any, Dict, List, Tuple, Optional

import numpy as np
import pandas as pd
import pytz
import requests
import yaml
import yfinance as yf
from ib_insync import IB, MarketOrder, Stock

try:
    import pandas_market_calendars as mcal
    _NYSE_CAL = mcal.get_calendar("NYSE")
except Exception:
    _NYSE_CAL = None

from db import db_connect, insert_trade, last_trades, cumulative_pnl_series, get_buy_time
from indicators import rsi_wilder
from strategy import EnhancedDipBuyStrategy

# =========================================================
# CONFIG LOADING
# =========================================================
def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

CONFIG_FILE = os.getenv("CONFIG_FILE", "/app/config.yaml")
CFG = load_config(CONFIG_FILE)

CAPITAL = CFG.get("capital", {})
STRAT = CFG.get("strategy", {})
RUNTIME = CFG.get("runtime", {})
REPORT_CFG = CFG.get("report", {})

# =========================================================
# ENV / INFRA
# =========================================================
IB_IP = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "4002"))
CLIENT_ID = int(os.getenv("CLIENT_ID", "2"))

DB_PATH = os.getenv("DB_PATH", "/data/algobot.db")
STATE_FILE = os.getenv("STATE_FILE", "/data/bot_state.json")

TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# --- NEW: Live status + live log for HTML dashboard (no backend needed)
LOG_FILE = os.getenv("LOG_FILE", "/data/bot.log")                # persistent log in data volume
STATUS_FILE = os.getenv("STATUS_FILE", "/reports/status.json")   # for dashboard
LOG_TAIL_FILE = os.getenv("LOG_TAIL_FILE", "/reports/log_tail.txt")

# --- Dashboard v2 snapshots (polling JSONs)
PORTFOLIO_JSON = os.getenv("PORTFOLIO_JSON", "/reports/portfolio.json")
TRADES_JSON = os.getenv("TRADES_JSON", "/reports/trades.json")
CANDIDATES_JSON = os.getenv("CANDIDATES_JSON", "/reports/candidates.json")
EQUITY_CURVE_JSON = os.getenv("EQUITY_CURVE_JSON", "/reports/equity_curve.json")
STRATEGY_STATE_JSON = os.getenv("STRATEGY_STATE_JSON", "/reports/strategy_state.json")
CONTROL_FILE = os.getenv("CONTROL_FILE", "/data/control.json")

# =========================================================
# PARAMS (from config.yaml)
# =========================================================
MANUAL_CAPITAL_LIMIT = float(CAPITAL.get("manual_capital_limit", 10000))
MAX_POSITIONS = int(CAPITAL.get("max_positions", 5))
FEE = float(CAPITAL.get("fee_usd", 1.0))

# Anti-duplicate SELL: cooldown per symbol after any sell attempt (filled or not)
SELL_COOLDOWN_SEC = 30 * 60
_recent_sell_attempts: Dict[str, float] = {}

DIP_MODE = str(STRAT.get("dip_mode", "DAILY")).upper()
BUY_DROP = float(STRAT.get("buy_drop", 0.02))
SELL_GAIN = float(STRAT.get("sell_gain", 0.03))

USE_STOP_LOSS = bool(STRAT.get("use_stop_loss", False))
STOP_LOSS = float(STRAT.get("stop_loss", 0.15))

RSI_LIMIT = float(STRAT.get("rsi_limit", 30))
RSI_PERIOD = int(STRAT.get("rsi_period", 14))

USE_SMA_FILTER = bool(STRAT.get("use_sma_filter", False))
SMA_PERIOD = int(STRAT.get("sma_period", 200))

NYC_TZ = pytz.timezone(str(RUNTIME.get("timezone", "US/Eastern")))
SP100_CACHE_FILE = str(RUNTIME.get("sp100_cache_file", "sp100_tickers_cache.txt"))
SP100_CACHE_MAX_AGE_HOURS = int(RUNTIME.get("sp100_cache_max_age_hours", 24))

SHOW_CANDIDATES_RSI_BELOW = float(REPORT_CFG.get("show_candidates_rsi_below", 60))
CANDIDATES_LIMIT = int(REPORT_CFG.get("candidates_limit", 15))
TRADES_TABLE_LIMIT = int(REPORT_CFG.get("trades_table_limit", 10))

# Dashboard refresh (UX)
DASH_REFRESH_OPEN_SEC = int(REPORT_CFG.get("dashboard_refresh_open_sec", 60))
DASH_REFRESH_CLOSED_SEC = int(REPORT_CFG.get("dashboard_refresh_closed_sec", 600))
DASH_REFRESH_ON_START = bool(REPORT_CFG.get("dashboard_refresh_on_start", True))

# --- NEW: live status + live log refresh knobs
LOG_TAIL_LINES = int(REPORT_CFG.get("log_tail_lines", 200))
STATUS_POLL_SEC = int(REPORT_CFG.get("status_poll_sec", 5))
LOG_POLL_SEC = int(REPORT_CFG.get("log_poll_sec", 5))

LAST_CANDIDATES_REPORT: List[Dict[str, Any]] = []
SMA_CACHE: Dict[str, Tuple[datetime.datetime, bool]] = {}

# =========================================================
# STRATEGY INSTANCE
# =========================================================
STRATEGY = EnhancedDipBuyStrategy({
    "dip_mode": DIP_MODE,
    "buy_drop": BUY_DROP,
    "sell_gain": SELL_GAIN,
    "rsi_limit": RSI_LIMIT,
    "rsi_period": RSI_PERIOD,
    "use_stop_loss": USE_STOP_LOSS,
    "stop_loss": STOP_LOSS,
    "use_sma_filter": USE_SMA_FILTER,
    "sma_period": SMA_PERIOD,
    "use_trailing_stop": bool(STRAT.get("use_trailing_stop", True)),
    "trailing_stop_pct": float(STRAT.get("trailing_stop_pct", 0.02)),
    "use_time_stop": bool(STRAT.get("use_time_stop", True)),
    "time_stop_bars": int(STRAT.get("time_stop_bars", 120)),
    "use_macd": bool(STRAT.get("use_macd", True)),
    "macd_fast": int(STRAT.get("macd_fast", 12)),
    "macd_slow": int(STRAT.get("macd_slow", 26)),
    "macd_signal": int(STRAT.get("macd_signal", 9)),
    "use_bollinger": bool(STRAT.get("use_bollinger", True)),
    "bb_period": int(STRAT.get("bb_period", 20)),
    "bb_std": float(STRAT.get("bb_std", 2.0)),
    "use_volume_filter": bool(STRAT.get("use_volume_filter", False)),
    "volume_multiplier": float(STRAT.get("volume_multiplier", 1.5)),
})

# =========================================================
# LOGGING (stdout + file + tail for dashboard)
# =========================================================
_log_tail = deque(maxlen=LOG_TAIL_LINES)
_last_error: Optional[str] = None

def _ensure_parent_dir(path: str) -> None:
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
    except Exception:
        pass

def _load_existing_tail() -> None:
    try:
        if os.path.exists(LOG_TAIL_FILE):
            with open(LOG_TAIL_FILE, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()[-LOG_TAIL_LINES:]
                for ln in lines:
                    _log_tail.append(ln)
    except Exception:
        pass

def _write_log_files(line: str) -> None:
    # persistent full log
    try:
        _ensure_parent_dir(LOG_FILE)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

    # tail log for dashboard
    try:
        _ensure_parent_dir(LOG_TAIL_FILE)
        _log_tail.append(line)
        with open(LOG_TAIL_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(_log_tail) + "\n")
    except Exception:
        pass

def log(msg: str, level: str = "INFO") -> None:
    global _last_error
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [{level}] {msg}"
    print(line, flush=True)
    _write_log_files(line)

    if level.upper() in ("ERROR", "CRITICAL"):
        _last_error = msg

# =========================================================
# TELEGRAM
# =========================================================
def send_telegram_msg(message: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        params = {"chat_id": TG_CHAT_ID, "text": message}
        requests.get(url, params=params, timeout=5)
    except Exception as e:
        log(f"TG CHYBA: {e}", "ERROR")

# =========================================================
# TIME MANAGEMENT
# =========================================================
def get_ny_time() -> datetime.datetime:
    return datetime.datetime.now(NYC_TZ)

def _nyse_schedule(start_day, end_day):
    """Return NYSE schedule (open/close per day) or None if calendar unavailable."""
    if _NYSE_CAL is None:
        return None
    try:
        return _NYSE_CAL.schedule(start_date=start_day, end_date=end_day)
    except Exception:
        return None

def is_market_open() -> bool:
    ny_now = get_ny_time()

    # Fast weekend reject (regardless of calendar availability)
    if ny_now.weekday() > 4:
        return False

    sched = _nyse_schedule(ny_now.date(), ny_now.date())
    if sched is not None and not sched.empty:
        # Use actual NYSE open/close (handles holidays + early closes 1pm dny)
        market_open = sched.iloc[0]["market_open"].tz_convert(NYC_TZ).to_pydatetime()
        market_close = sched.iloc[0]["market_close"].tz_convert(NYC_TZ).to_pydatetime()
        return market_open <= ny_now <= market_close
    if sched is not None and sched.empty:
        # Holiday — NYSE má prázdný rozvrh pro tento den
        return False

    # Fallback (calendar import failed): původní weekday + 9:30–16:00 logika
    market_open = ny_now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = ny_now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= ny_now <= market_close

def seconds_until_market_open() -> int:
    ny_now = get_ny_time()

    # Use NYSE calendar if available — handles holidays correctly
    if _NYSE_CAL is not None:
        try:
            end = (ny_now + datetime.timedelta(days=14)).date()
            sched = _NYSE_CAL.schedule(start_date=ny_now.date(), end_date=end)
            for _, row in sched.iterrows():
                m_open = row["market_open"].tz_convert(NYC_TZ).to_pydatetime()
                if m_open > ny_now:
                    return max(60, int((m_open - ny_now).total_seconds()))
        except Exception:
            pass

    # Fallback (calendar failed): weekday-only logic
    today_open = ny_now.replace(hour=9, minute=30, second=0, microsecond=0)
    if ny_now < today_open and ny_now.weekday() <= 4:
        return max(60, int((today_open - ny_now).total_seconds()))

    next_day = ny_now + datetime.timedelta(days=1)
    while next_day.weekday() > 4:
        next_day += datetime.timedelta(days=1)

    next_open = next_day.replace(hour=9, minute=30, second=0, microsecond=0)
    return max(60, int((next_open - ny_now).total_seconds()))

def seconds_until(dt_target: datetime.datetime) -> int:
    now = get_ny_time()
    return max(1, int((dt_target - now).total_seconds()))

def fmt_hms(seconds: int) -> str:
    seconds = int(max(0, seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def countdown_sleep(seconds: int, prefix: str) -> None:
    seconds = int(max(0, seconds))
    if sys.stdout.isatty():
        while seconds > 0:
            h = seconds // 3600
            m = (seconds % 3600) // 60
            s = seconds % 60
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\r{ts} [WAIT] {prefix} {h:02d}:{m:02d}:{s:02d}   ", end="", flush=True)
            time.sleep(1)
            seconds -= 1
        print()
        return

    log(f"{prefix} {fmt_hms(seconds)}")
    time.sleep(seconds)

# =========================================================
# SCHEDULING
# =========================================================
def in_sell_window(now_ny: datetime.datetime) -> bool:
    start = now_ny.replace(hour=9, minute=35, second=0, microsecond=0)
    end = now_ny.replace(hour=15, minute=55, second=59, microsecond=999999)
    return start <= now_ny <= end

def in_buy_window(now_ny: datetime.datetime) -> bool:
    if not (10 <= now_ny.hour <= 15):
        return False
    return (31 <= now_ny.minute <= 33)

def sell_cycle_id_5min(now_ny: datetime.datetime) -> str:
    bucket_min = (now_ny.minute // 5) * 5
    return f"{now_ny.strftime('%Y-%m-%d')}-{now_ny.hour:02d}-{bucket_min:02d}"

def buy_cycle_id_hour(now_ny: datetime.datetime) -> str:
    return f"{now_ny.strftime('%Y-%m-%d')}-{now_ny.hour:02d}"

def next_5min_boundary(now_ny: datetime.datetime) -> datetime.datetime:
    base = now_ny.replace(second=0, microsecond=0)
    add = 5 - (base.minute % 5)
    if add == 5 and now_ny.second == 0 and now_ny.microsecond == 0:
        add = 0
    target = base + datetime.timedelta(minutes=add)
    if target <= now_ny:
        target = target + datetime.timedelta(minutes=5)
    return target

def next_buy_run_time(now_ny: datetime.datetime) -> datetime.datetime:
    if now_ny.weekday() > 4:
        d = now_ny + datetime.timedelta(days=1)
        while d.weekday() > 4:
            d += datetime.timedelta(days=1)
        return d.replace(hour=10, minute=31, second=0, microsecond=0)

    if now_ny.hour < 10:
        return now_ny.replace(hour=10, minute=31, second=0, microsecond=0)

    if 10 <= now_ny.hour <= 15:
        run = now_ny.replace(minute=31, second=0, microsecond=0)
        if now_ny < run:
            return run
        if now_ny.hour < 15:
            return (now_ny + datetime.timedelta(hours=1)).replace(minute=31, second=0, microsecond=0)

    d = now_ny + datetime.timedelta(days=1)
    while d.weekday() > 4:
        d += datetime.timedelta(days=1)
    return d.replace(hour=10, minute=31, second=0, microsecond=0)

def next_sell_run_time(now_ny: datetime.datetime) -> datetime.datetime:
    if now_ny.weekday() > 4:
        d = now_ny + datetime.timedelta(days=1)
        while d.weekday() > 4:
            d += datetime.timedelta(days=1)
        return d.replace(hour=9, minute=35, second=0, microsecond=0)

    start = now_ny.replace(hour=9, minute=35, second=0, microsecond=0)
    end = now_ny.replace(hour=15, minute=55, second=0, microsecond=0)

    if now_ny < start:
        return start

    if now_ny > end:
        d = now_ny + datetime.timedelta(days=1)
        while d.weekday() > 4:
            d += datetime.timedelta(days=1)
        return d.replace(hour=9, minute=35, second=0, microsecond=0)

    nxt = next_5min_boundary(now_ny)
    if nxt > end:
        d = now_ny + datetime.timedelta(days=1)
        while d.weekday() > 4:
            d += datetime.timedelta(days=1)
        return d.replace(hour=9, minute=35, second=0, microsecond=0)
    return nxt

# =========================================================
# STATE
# =========================================================
def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def save_state(state: dict) -> None:
    try:
        state["saved_at"] = datetime.datetime.now().isoformat()
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        log(f"STATE save error: {e}", "ERROR")

# =========================================================
# LIVE STATUS FILE (for dashboard)
# =========================================================
def write_status(
    *,
    market_open: bool,
    ib_connected: bool,
    ny_time: datetime.datetime,
    secs_to_open: Optional[int],
    next_sell: Optional[datetime.datetime],
    next_buy: Optional[datetime.datetime],
    equity: Optional[float],
    cash: Optional[float],
    positions_count: Optional[int],
    last_action: Optional[str] = None,
) -> None:
    payload = {
        "ts_local": datetime.datetime.now().isoformat(timespec="seconds"),
        "ts_ny": ny_time.isoformat(timespec="seconds"),
        "market_open": market_open,
        "ib_connected": ib_connected,
        "secs_to_open": secs_to_open,
        "next_sell_ny": next_sell.isoformat(timespec="seconds") if next_sell else None,
        "next_buy_ny": next_buy.isoformat(timespec="seconds") if next_buy else None,
        "equity": equity,
        "cash": cash,
        "positions_count": positions_count,
        "last_action": last_action,
        "last_error": _last_error,
        "version": "2026-01-09",
    }
    try:
        _ensure_parent_dir(STATUS_FILE)
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# =========================================================
# DASHBOARD V2 — JSON SNAPSHOTS + CONTROL
# =========================================================

# Sparkline cache: symbol -> (fetched_at, [last 24 hourly close prices])
_SPARK_CACHE: Dict[str, Tuple[datetime.datetime, List[float]]] = {}
SPARK_CACHE_MAX_AGE_MIN = 10


def _atomic_write_json(path: str, payload: Any) -> None:
    try:
        _ensure_parent_dir(path)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as e:
        log(f"v2 write error {path}: {e}", "WARNING")


def read_control() -> Dict[str, Any]:
    """Read dashboard v2 control state (pause toggle written by web service)."""
    try:
        if not os.path.exists(CONTROL_FILE):
            return {}
        with open(CONTROL_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def get_sparkline(symbol: str, n: int = 24) -> List[float]:
    """Return last N hourly close prices for symbol. Cached for SPARK_CACHE_MAX_AGE_MIN min."""
    now = datetime.datetime.now()
    cached = _SPARK_CACHE.get(symbol)
    if cached and (now - cached[0]).total_seconds() < SPARK_CACHE_MAX_AGE_MIN * 60:
        return cached[1]
    try:
        yf_sym = symbol.replace(' ', '-')
        df = yf.download(yf_sym, period="3d", interval="1h",
                         progress=False, auto_adjust=True, threads=False)
        if df.empty:
            _SPARK_CACHE[symbol] = (now, [])
            return []
        if isinstance(df.columns, pd.MultiIndex):
            try:
                df.columns = df.columns.get_level_values(0)
            except Exception:
                pass
        series = df['Close'] if 'Close' in df.columns else df.iloc[:, 0]
        vals = []
        for v in series.tail(n).tolist():
            try:
                fv = float(v)
                if not np.isnan(fv):
                    vals.append(round(fv, 4))
            except Exception:
                pass
        _SPARK_CACHE[symbol] = (now, vals)
        return vals
    except Exception:
        _SPARK_CACHE[symbol] = (now, [])
        return []


def _exit_progress(pos_symbol: str, avg_cost: float, curr_price: float,
                   holding_bars: int) -> Dict[str, float]:
    """Return progress to each exit trigger as 0..100 percent.

    100 % = trigger fires. Used for position card progress bars in v2 dashboard.
    """
    pnl_pct = (curr_price - avg_cost) / avg_cost if avg_cost else 0.0

    # Stop loss: progress = how close PnL is to -stop_loss (only if PnL is negative)
    if USE_STOP_LOSS and STOP_LOSS > 0:
        sl_pct = max(0.0, min(100.0, (-pnl_pct / STOP_LOSS) * 100.0)) if pnl_pct < 0 else 0.0
    else:
        sl_pct = 0.0

    # Trailing: how close current price is to trailing-stop trigger from HWM
    trail_pct_param = float(STRAT.get("trailing_stop_pct", 0.02))
    hwm = STRATEGY._high_water_marks.get(pos_symbol) if hasattr(STRATEGY, "_high_water_marks") else None
    if hwm and trail_pct_param > 0:
        drop_from_hwm = max(0.0, (hwm - curr_price) / hwm)
        ts_pct = max(0.0, min(100.0, (drop_from_hwm / trail_pct_param) * 100.0))
    else:
        ts_pct = 0.0

    # Time stop
    tsb = int(STRAT.get("time_stop_bars", 240))
    time_pct = max(0.0, min(100.0, (holding_bars / tsb) * 100.0)) if tsb else 0.0

    return {
        "stop_loss_progress": round(sl_pct, 1),
        "trailing_stop_progress": round(ts_pct, 1),
        "time_stop_progress": round(time_pct, 1),
        "hwm": float(hwm) if hwm else None,
    }


def write_portfolio_json(conn, portfolio_data: List[Dict[str, Any]]) -> None:
    """Write enriched portfolio snapshot with sparklines and exit-trigger progress."""
    positions = []
    for p in portfolio_data:
        sym = p.get("symbol", "")
        avg = float(p.get("avgCost") or 0.0)
        curr = float(p.get("marketPrice") or 0.0)
        qty = int(p.get("qty") or 0)
        holding_bars = _estimate_holding_hours(conn, sym) if sym else 0

        triggers = _exit_progress(sym, avg, curr, holding_bars)
        spark = get_sparkline(sym, 24) if sym and curr > 0 else []

        positions.append({
            "symbol": sym,
            "qty": qty,
            "avgCost": round(avg, 4),
            "marketPrice": round(curr, 4),
            "pnl_pct": round(float(p.get("pnl_pct") or 0.0), 4),
            "holding_bars": holding_bars,
            "sparkline": spark,
            **triggers,
        })

    payload = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "positions": positions,
    }
    _atomic_write_json(PORTFOLIO_JSON, payload)


def write_trades_json(conn, limit: int = 50) -> None:
    try:
        rows = last_trades(conn, limit)
    except Exception as e:
        log(f"v2 trades read error: {e}", "WARNING")
        rows = []
    payload = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "items": rows,
    }
    _atomic_write_json(TRADES_JSON, payload)


def write_candidates_json(candidates: List[Dict[str, Any]]) -> None:
    enriched = []
    for c in candidates:
        rsi = float(c.get("rsi") or 0.0)
        # Loose score proxy: 1 - rsi/100 (lower RSI = higher score). is_buy_signal already
        # reflects Enhanced strategy's full score; if signal flagged, bump score.
        base = max(0.0, min(1.0, (50.0 - rsi) / 50.0)) if rsi > 0 else 0.0
        if c.get("is_buy_signal"):
            base = max(base, 0.75)
        enriched.append({
            "symbol": c.get("symbol", ""),
            "price": float(c.get("price") or 0.0),
            "rsi": round(rsi, 2),
            "drop": round(float(c.get("drop") or 0.0), 4),
            "score": round(base, 3),
            "is_buy_signal": bool(c.get("is_buy_signal")),
            "sma_ok": bool(c.get("sma_ok", True)),
        })
    payload = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "items": enriched,
    }
    _atomic_write_json(CANDIDATES_JSON, payload)


def write_equity_curve_json(conn) -> None:
    """Build equity curve + drawdown series from DB cumulative PnL."""
    try:
        dates, vals = cumulative_pnl_series(conn)
    except Exception as e:
        log(f"v2 equity read error: {e}", "WARNING")
        dates, vals = [], []

    base = float(MANUAL_CAPITAL_LIMIT) if MANUAL_CAPITAL_LIMIT > 0 else 10000.0
    points = []
    peak_equity = base
    max_dd_pct = 0.0
    for d, v in zip(dates, vals):
        cum_pnl = float(v or 0.0)
        equity = base + cum_pnl
        if equity > peak_equity:
            peak_equity = equity
        dd_pct = ((equity - peak_equity) / peak_equity) * 100.0 if peak_equity > 0 else 0.0
        if dd_pct < max_dd_pct:
            max_dd_pct = dd_pct
        roi_pct = (cum_pnl / base) * 100.0 if base > 0 else 0.0
        # date string from db is ISO-ish; ensure parseable
        try:
            iso = datetime.datetime.fromisoformat(d.replace(" ", "T")[:19]).isoformat()
        except Exception:
            iso = d
        points.append({
            "t": iso,
            "equity": round(equity, 2),
            "cumulative_pnl": round(cum_pnl, 2),
            "drawdown_pct": round(dd_pct, 3),
            "roi_pct": round(roi_pct, 3),
        })

    payload = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "base_capital": base,
        "max_drawdown_pct": round(abs(max_dd_pct), 3),
        "points": points,
    }
    _atomic_write_json(EQUITY_CURVE_JSON, payload)


def write_strategy_state_json(conn) -> None:
    ctrl = read_control()
    # daily realized PnL from trades today
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS s, COUNT(*) AS c "
            "FROM trades WHERE ts LIKE ?",
            (today + "%",)
        ).fetchone()
        daily_pnl = float(row["s"] or 0.0)
        daily_count = int(row["c"] or 0)
    except Exception:
        daily_pnl = 0.0
        daily_count = 0

    # cooldowns from _recent_sell_attempts: keep only ones still active
    now = time.time()
    cooldowns = []
    for sym, ts in list(_recent_sell_attempts.items()):
        rem = SELL_COOLDOWN_SEC - (now - ts)
        if rem > 0:
            cooldowns.append({"symbol": sym, "remaining_sec": int(rem)})

    # last cycle ids from state file
    try:
        st = load_state()
        last_buy_cycle = st.get("last_buy_cycle_id", "")
        last_sell_cycle = st.get("last_sell_cycle_id", "")
    except Exception:
        last_buy_cycle = ""
        last_sell_cycle = ""

    # determine paper/live by IB port (4002 paper / 4001 live) — best-effort label
    mode = "live" if IB_PORT == 4001 else "paper"

    payload = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "name": STRATEGY.name,
        "mode": mode,
        "paused": bool(ctrl.get("paused", False)),
        "max_positions": MAX_POSITIONS,
        "daily_realized_pnl": round(daily_pnl, 2),
        "daily_trades_count": daily_count,
        "daily_loss_limit_pct": 3.0,  # placeholder until B2 ships
        "last_buy_cycle": last_buy_cycle,
        "last_sell_cycle": last_sell_cycle,
        "cooldowns": cooldowns,
        "params": {
            "buy_drop": BUY_DROP,
            "sell_gain": SELL_GAIN,
            "stop_loss": STOP_LOSS if USE_STOP_LOSS else None,
            "rsi_limit": RSI_LIMIT,
            "trailing_stop_pct": float(STRAT.get("trailing_stop_pct", 0.0)) if STRAT.get("use_trailing_stop") else None,
            "time_stop_bars": int(STRAT.get("time_stop_bars", 0)) if STRAT.get("use_time_stop") else None,
            "dip_mode": DIP_MODE,
        },
    }
    _atomic_write_json(STRATEGY_STATE_JSON, payload)


def write_v2_snapshots(conn, portfolio_data: List[Dict[str, Any]],
                       candidates: Optional[List[Dict[str, Any]]] = None) -> None:
    """Write all v2 dashboard JSON snapshots in one call."""
    try:
        write_portfolio_json(conn, portfolio_data or [])
        write_trades_json(conn, limit=50)
        write_candidates_json(candidates if candidates is not None else LAST_CANDIDATES_REPORT)
        write_equity_curve_json(conn)
        write_strategy_state_json(conn)
    except Exception as e:
        log(f"v2 snapshots write error: {e}", "WARNING")


# =========================================================
# SP100 (Wikipedia + cache)
# =========================================================
def _read_cached_sp100():
    try:
        if not os.path.exists(SP100_CACHE_FILE):
            return None
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(SP100_CACHE_FILE))
        age_h = (datetime.datetime.now() - mtime).total_seconds() / 3600.0
        if age_h > SP100_CACHE_MAX_AGE_HOURS:
            return None
        with open(SP100_CACHE_FILE, "r", encoding="utf-8") as f:
            tickers = [line.strip() for line in f if line.strip()]
        return tickers if tickers else None
    except Exception:
        return None

def _write_cached_sp100(tickers):
    try:
        with open(SP100_CACHE_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(tickers) + "\n")
    except Exception as e:
        log(f"SP100 cache write error: {e}", "WARNING")

def get_sp100_tickers():
    fallback_list = ['AAPL', 'MSFT', 'GOOG', 'AMZN', 'NVDA', 'META', 'JPM', 'WMT', 'PG', 'XOM']

    cached = _read_cached_sp100()
    if cached:
        return cached

    url = "https://en.wikipedia.org/wiki/S%26P_100"
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120 Safari/537.36"}

    try:
        resp = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        if resp.status_code != 200:
            log(f"SP100 HTTP {resp.status_code}. Používám fallback.", "WARNING")
            return fallback_list

        tables = pd.read_html(StringIO(resp.text))
        df = next((t for t in tables if 'Symbol' in t.columns), None)
        if df is None:
            return fallback_list

        tickers = [str(s).strip().replace('.', '-') for s in df['Symbol'].tolist()]
        tickers = [t for t in tickers if t]

        if len(tickers) < 50:
            return fallback_list

        _write_cached_sp100(tickers)
        log(f"SP100: načteno {len(tickers)} tickerů (uloženo do cache).")
        return tickers

    except Exception as e:
        log(f"SP100 chyba: {e}. Používám fallback.", "WARNING")
        return fallback_list

# =========================================================
# INDICATORS
# =========================================================
def yf_to_ib_symbol(symbol: str) -> str:
    if symbol == 'BRK-B':
        return 'BRK B'
    return symbol.replace('-', ' ')

def calculate_rsi_wilder(series: pd.Series, period: int = 14) -> pd.Series:
    """Wrapper for backwards compatibility. Uses indicators module."""
    return rsi_wilder(series, period)

def check_daily_sma200(symbol: str, period: int = 200, max_age_hours: int = 12) -> bool:
    now = datetime.datetime.now(datetime.timezone.utc)
    cached = SMA_CACHE.get(symbol)
    if cached:
        ts, passed = cached
        age_h = (now - ts).total_seconds() / 3600.0
        if age_h <= max_age_hours:
            return passed

    try:
        df = yf.download(symbol, period="2y", interval="1d", progress=False, auto_adjust=True, threads=False)
        if df.empty or len(df) < period:
            SMA_CACHE[symbol] = (now, False)
            return False

        if isinstance(df.columns, pd.MultiIndex):
            try:
                df.columns = df.columns.get_level_values(0)
            except Exception:
                pass

        series = df['Close'] if 'Close' in df.columns else df.iloc[:, 0]
        sma = series.rolling(window=period).mean().iloc[-1]
        curr = series.iloc[-1]
        if np.isnan(sma) or np.isnan(curr):
            return False

        passed = bool(curr > sma)
        SMA_CACHE[symbol] = (now, passed)
        return passed

    except Exception as e:
        log(f"SMA CRASH {symbol}: {e}", "ERROR")
        SMA_CACHE[symbol] = (now, False)
        return False

def get_current_price(symbol: str, ib_contract=None, ib_obj: IB = None):
    try:
        yf_symbol = symbol.replace(' ', '-')
        ticker = yf.Ticker(yf_symbol)
        price = ticker.fast_info.get('last_price', None)
        if price is not None and price > 0:
            return float(price)
    except Exception:
        pass

    try:
        yf_symbol = symbol.replace(' ', '-')
        ticker = yf.Ticker(yf_symbol)
        hist = ticker.history(period="1d", interval="1m")
        if not hist.empty:
            price = float(hist['Close'].iloc[-1])
            if price > 0:
                return price
    except Exception:
        pass

    if ib_obj and ib_contract and ib_obj.isConnected():
        try:
            t = ib_obj.reqMktData(ib_contract, "", snapshot=True, regulatorySnapshot=False)
            ib_obj.sleep(1.0)
            price = t.marketPrice()
            ib_obj.cancelMktData(ib_contract)
            if price and not np.isnan(price) and price > 0:
                return float(price)
        except Exception:
            pass

    return None

# =========================================================
# ACCOUNT
# =========================================================
def read_account_summary(ib: IB):
    cash = 0.0
    equity = 0.0
    try:
        summary = ib.accountSummary()
        for v in summary:
            if v.tag == 'TotalCashValue':
                cash = float(v.value)
            if v.tag == 'NetLiquidation':
                equity = float(v.value)
    except Exception as e:
        log(f"Nelze načíst account summary: {e}", "ERROR")
    return cash, equity

def dump_positions(ib: IB, header: str):
    try:
        ib.reqPositions()
        ib.sleep(1.0)
        pos = ib.positions()
        log(f"{header} Pozic: {len(pos)}")
        for p in pos:
            log(f"  {p.contract.symbol} qty={p.position} avgCost={p.avgCost}")
    except Exception as e:
        log(f"Dump positions error: {e}", "ERROR")

# =========================================================
# DB log
# =========================================================
def log_trade(conn, action, symbol, price, qty, pnl=0.0, note=""):
    insert_trade(conn, action, symbol, price, qty, pnl, note)


# =========================================================
# DASHBOARD REFRESH (works even when market closed)
# =========================================================
def refresh_dashboard(conn, ib: IB, last_action: Optional[str] = None) -> None:
    now_ny = get_ny_time()
    market_open = is_market_open()

    next_sell = next_sell_run_time(now_ny)
    next_buy = next_buy_run_time(now_ny)
    secs_to_open = 0 if market_open else seconds_until_market_open()

    if not ib.isConnected():
        try:
            ib.connect(IB_IP, IB_PORT, clientId=CLIENT_ID, timeout=15)
            ib.reqMarketDataType(3)  # delayed
        except Exception as e:
            write_status(
                market_open=market_open,
                ib_connected=False,
                ny_time=now_ny,
                secs_to_open=secs_to_open,
                next_sell=next_sell,
                next_buy=next_buy,
                equity=None,
                cash=None,
                positions_count=None,
                last_action=last_action,
            )
            raise e

    account_cash, portfolio_equity = read_account_summary(ib)

    portfolio_data_latest = []
    ib.reqPositions()
    ib.sleep(0.5)
    for pos in ib.positions():
        cp = get_current_price(pos.contract.symbol, pos.contract, ib) or pos.avgCost
        pp = (cp - pos.avgCost) / pos.avgCost if pos.avgCost else 0
        portfolio_data_latest.append({
            'symbol': pos.contract.symbol,
            'qty': pos.position,
            'avgCost': pos.avgCost,
            'marketPrice': cp,
            'pnl_pct': pp
        })

    write_status(
        market_open=market_open,
        ib_connected=ib.isConnected(),
        ny_time=now_ny,
        secs_to_open=secs_to_open,
        next_sell=next_sell,
        next_buy=next_buy,
        equity=portfolio_equity,
        cash=account_cash,
        positions_count=len(portfolio_data_latest),
        last_action=last_action,
    )

    # Dashboard JSON snapshots — read by polling UI in /reports/index.html
    write_v2_snapshots(conn, portfolio_data_latest)

# =========================================================
# CORE: SELL
# =========================================================
def _estimate_holding_hours(conn, symbol: str) -> int:
    """Count NYSE trading hours since the position was opened.

    Matches the backtest semantic where 1 bar == 1 hourly trading bar.
    Falls back to wall-clock hours if NYSE calendar is unavailable, but logs the divergence.
    """
    buy_ts = get_buy_time(conn, symbol)
    if not buy_ts:
        return 0
    try:
        buy_dt = datetime.datetime.fromisoformat(buy_ts)
        now = datetime.datetime.now()
        if now <= buy_dt:
            return 0

        if _NYSE_CAL is not None:
            try:
                sched = _NYSE_CAL.schedule(start_date=buy_dt.date(), end_date=now.date())
                total = 0.0
                for _, row in sched.iterrows():
                    m_open = row["market_open"].tz_convert(NYC_TZ).to_pydatetime().replace(tzinfo=None)
                    m_close = row["market_close"].tz_convert(NYC_TZ).to_pydatetime().replace(tzinfo=None)
                    # Clip to [buy_dt, now]
                    start = max(m_open, buy_dt)
                    end = min(m_close, now)
                    if end > start:
                        total += (end - start).total_seconds() / 3600.0
                return max(0, int(total))
            except Exception:
                pass

        hours = int((now - buy_dt).total_seconds() / 3600)
        return max(0, hours)
    except Exception:
        return 0


def manage_positions_sell_only(conn, ib: IB):
    ib.reqPositions()
    ib.sleep(0.5)
    current_positions = ib.positions()

    positions_changed = False
    portfolio_data = []

    log(f"SELL-CHECK: pozic {len(current_positions)} (strategie: {STRATEGY.name})")

    for pos in current_positions:
        contract = pos.contract
        curr_price = get_current_price(contract.symbol, contract, ib)

        if curr_price is None or curr_price <= 0:
            portfolio_data.append({
                'symbol': contract.symbol,
                'qty': pos.position,
                'avgCost': pos.avgCost,
                'marketPrice': 0,
                'pnl_pct': 0
            })
            log(f"{contract.symbol} Nelze zjistit cenu", "WARNING")
            continue

        pnl_pct = (curr_price - pos.avgCost) / pos.avgCost if pos.avgCost else 0
        holding_bars = _estimate_holding_hours(conn, contract.symbol)

        # Use Enhanced strategy for exit decision
        exit_signal = STRATEGY.should_exit(
            contract.symbol, pos.avgCost, curr_price, holding_bars
        )

        reason = exit_signal.reason if exit_signal else ""
        log(f"{contract.symbol} PnL {pnl_pct*100:+.2f}% (Cena {curr_price:.2f}, hold {holding_bars}h){' -> ' + reason if reason else ''}")

        if exit_signal:
            # Cooldown: pokud jsme se v posledních SELL_COOLDOWN_SEC pokusili prodat
            # (a order se nepotvrdil), neopakuj. Brání duplicitním phantom SELL.
            last_attempt = _recent_sell_attempts.get(contract.symbol, 0.0)
            if time.time() - last_attempt < SELL_COOLDOWN_SEC:
                remaining = int(SELL_COOLDOWN_SEC - (time.time() - last_attempt))
                log(f"{contract.symbol} SELL skip (cooldown {remaining}s po předchozím pokusu)", "WARNING")
                portfolio_data.append({
                    'symbol': contract.symbol,
                    'qty': pos.position,
                    'avgCost': pos.avgCost,
                    'marketPrice': curr_price,
                    'pnl_pct': pnl_pct
                })
                continue

            # Resync open orders ze serveru (důležité po reconnectu)
            try:
                ib.reqAllOpenOrders()
                ib.sleep(0.3)
                if any(t.contract.symbol == contract.symbol and t.order.action == 'SELL' for t in ib.openTrades()):
                    log(f"{contract.symbol} SELL už existuje v openOrders, skip", "WARNING")
                    _recent_sell_attempts[contract.symbol] = time.time()
                    portfolio_data.append({
                        'symbol': contract.symbol,
                        'qty': pos.position,
                        'avgCost': pos.avgCost,
                        'marketPrice': curr_price,
                        'pnl_pct': pnl_pct
                    })
                    continue
            except Exception as e:
                log(f"{contract.symbol} openOrders check failed: {e}", "WARNING")

            sell_contract = Stock(contract.symbol, 'SMART', 'USD')
            order = MarketOrder('SELL', pos.position, tif='DAY')
            trade = ib.placeOrder(sell_contract, order)
            _recent_sell_attempts[contract.symbol] = time.time()

            # Čekej na fill nebo terminální stav (až 8 s)
            deadline = time.time() + 8.0
            while time.time() < deadline:
                ib.sleep(0.5)
                st = trade.orderStatus.status
                if st in ('Filled', 'Cancelled', 'Inactive', 'ApiCancelled'):
                    break

            status = trade.orderStatus.status
            filled_qty = int(trade.orderStatus.filled or 0)
            fill_price = float(trade.orderStatus.avgFillPrice or 0.0)
            if fill_price <= 0:
                fill_price = curr_price

            if status == 'Filled' and filled_qty > 0:
                realized = (fill_price - pos.avgCost) * filled_qty - FEE
                log(f"TRADE SELL {contract.symbol} qty={filled_qty} price={fill_price:.2f} realized=${realized:.2f} reason={reason} [FILLED]")
                log_trade(conn, 'SELL', contract.symbol, fill_price, filled_qty, float(realized), reason)
                send_telegram_msg(f"SELL {contract.symbol} ({reason}) PnL ${realized:.2f}")
                positions_changed = True
            else:
                # Order NEPROŠEL → žádný DB zápis, žádný Telegram trade alert.
                # Cooldown už je nastavený, příští cyklus to nezopakuje.
                log(f"{contract.symbol} SELL nepotvrzen (status={status}, filled={filled_qty}) — bez DB zápisu, cooldown {SELL_COOLDOWN_SEC//60} min", "ERROR")
                send_telegram_msg(f"WARN: SELL {contract.symbol} nepotvrzen ({status}) — ručně zkontroluj IB")
                portfolio_data.append({
                    'symbol': contract.symbol,
                    'qty': pos.position,
                    'avgCost': pos.avgCost,
                    'marketPrice': curr_price,
                    'pnl_pct': pnl_pct
                })
        else:
            portfolio_data.append({
                'symbol': contract.symbol,
                'qty': pos.position,
                'avgCost': pos.avgCost,
                'marketPrice': curr_price,
                'pnl_pct': pnl_pct
            })

    return positions_changed, portfolio_data

# =========================================================
# CORE: BUY SCAN
# =========================================================
def scan_and_buy(conn, ib: IB, account_cash: float, portfolio_equity: float):
    candidates_report = []
    positions_changed = False

    # Dashboard v2 pause toggle — skip BUY scan entirely when paused.
    ctrl = read_control()
    if ctrl.get("paused"):
        log("BUY-SCAN: SKIP — obchodování PAUSOVÁNO z dashboardu")
        return False, candidates_report

    ib.reqPositions()
    ib.sleep(0.5)
    current_positions = ib.positions()

    if len(current_positions) >= MAX_POSITIONS:
        log(f"BUY-SCAN: portfolio plné ({len(current_positions)}/{MAX_POSITIONS})")
        return False, candidates_report

    tickers = get_sp100_tickers()
    log(f"BUY-SCAN: stahuji data pro {len(tickers)} tickerů (strategie: {STRATEGY.name})")

    try:
        data = yf.download(
            tickers,
            period="7d",
            interval="1h",
            progress=False,
            group_by='ticker',
            auto_adjust=True,
            threads=False
        )

        capital_base = MANUAL_CAPITAL_LIMIT if MANUAL_CAPITAL_LIMIT else portfolio_equity
        position_size_usd = capital_base / MAX_POSITIONS if MAX_POSITIONS else capital_base

        # Build per-symbol DataFrames for strategy
        symbol_data = {}
        for t in tickers:
            try:
                df = data[t] if isinstance(data.columns, pd.MultiIndex) else data
                df = df.dropna(subset=['Close'])
                if len(df) < RSI_PERIOD + 3:
                    continue
                symbol_data[t] = df
            except Exception:
                continue

        # Build candidates report (for dashboard watchlist)
        for t, df in symbol_data.items():
            try:
                curr = float(df['Close'].iloc[-1])
                rsi_val = float(calculate_rsi_wilder(df['Close'], RSI_PERIOD).iloc[-1])
                if np.isnan(rsi_val):
                    continue

                if DIP_MODE == "DAILY":
                    last_bar_date = df.index[-1].date()
                    prev_days_df = df[df.index.date < last_bar_date]
                    reference_price = float(prev_days_df['Close'].iloc[-1]) if not prev_days_df.empty else float(df['Close'].iloc[-2])
                else:
                    reference_price = float(df['Close'].iloc[-2])
                if not reference_price:
                    continue
                drop = (curr - reference_price) / reference_price

                sma_passed = True
                if USE_SMA_FILTER:
                    sma_passed = check_daily_sma200(t, SMA_PERIOD)

                if rsi_val < SHOW_CANDIDATES_RSI_BELOW:
                    candidates_report.append({
                        'symbol': t,
                        'price': curr,
                        'rsi': rsi_val,
                        'drop': drop,
                        'is_buy_signal': False,  # will be updated below
                        'sma_ok': sma_passed
                    })
            except Exception:
                continue

        # Use Enhanced strategy for signal generation
        existing_symbols = [p.contract.symbol for p in current_positions]
        # Also map yf symbols to IB symbols for dedup
        existing_yf = [s.replace(' ', '-') for s in existing_symbols]
        signals = STRATEGY.generate_signals(symbol_data, existing_symbols + existing_yf)

        # Mark buy signals in candidates report
        signal_symbols = {s.symbol for s in signals}
        for c in candidates_report:
            if c['symbol'] in signal_symbols:
                c['is_buy_signal'] = True

        if not signals:
            log(f"BUY-SCAN: žádný signál (kandidátů do reportu {len(candidates_report)})")
            return False, candidates_report

        # Take best signal
        top = signals[0]
        ib_sym = yf_to_ib_symbol(top.symbol)

        has_pos = any(p.contract.symbol == ib_sym for p in current_positions)
        try:
            has_ord = any(t.contract.symbol == ib_sym and t.order.action == 'BUY' for t in ib.openTrades())
        except Exception:
            has_ord = False

        if has_pos or has_ord:
            log(f"BUY-SCAN: SKIP {ib_sym} už v portfoliu nebo v openOrders", "WARNING")
            return False, candidates_report

        qty = int((position_size_usd / top.price) // 1)
        est_cost = qty * top.price

        if qty <= 0:
            log("BUY-SCAN: SKIP qty=0", "WARNING")
            return False, candidates_report

        if account_cash <= est_cost:
            log(f"BUY-SCAN: SKIP nedostatek hotovosti (cash ${account_cash:.0f} < est ${est_cost:.0f})", "WARNING")
            return False, candidates_report

        log(f"BUY {ib_sym} | {top.reason} | qty={qty}")
        contract = Stock(ib_sym, 'SMART', 'USD')
        order = MarketOrder('BUY', qty, tif='DAY')
        ib.placeOrder(contract, order)

        note_text = f"Enhanced: {top.reason}"
        log(f"TRADE BUY {ib_sym} qty={qty} price={float(top.price):.2f} fee=${FEE:.2f} note={note_text}")
        log_trade(conn, 'BUY', ib_sym, float(top.price), int(qty), -FEE, note_text)
        send_telegram_msg(f"BUY {ib_sym} ({note_text})")
        positions_changed = True

        return positions_changed, candidates_report

    except Exception as e:
        log(f"BUY-SCAN DATA ERROR: {e}", "ERROR")
        return False, candidates_report

# =========================================================
# MAIN LOOP
# =========================================================
def main_loop():
    global LAST_CANDIDATES_REPORT

    conn = db_connect(DB_PATH)

    _load_existing_tail()

    log("STARTUJI ALGO-BOT (SELL každých 5 min, BUY 1x/h v 10:31–15:33 NY)")
    send_telegram_msg("AlgoBot start")

    ib = IB()
    alert_sent = False
    did_startup_dump = False

    try:
        now_ny = get_ny_time()
        market_open = is_market_open()
        write_status(
            market_open=market_open,
            ib_connected=False,
            ny_time=now_ny,
            secs_to_open=(0 if market_open else seconds_until_market_open()),
            next_sell=next_sell_run_time(now_ny),
            next_buy=next_buy_run_time(now_ny),
            equity=None,
            cash=None,
            positions_count=None,
            last_action="START",
        )
    except Exception:
        pass

    if DASH_REFRESH_ON_START:
        try:
            refresh_dashboard(conn, ib, last_action="STARTUP_REFRESH")
            log("Dashboard inicializován hned po startu.")
        except Exception as e:
            log(f"Dashboard init error: {e}", "ERROR")
            try:
                if ib.isConnected():
                    ib.disconnect()
            except Exception:
                pass

    while True:
        try:
            now_ny = get_ny_time()

            if is_market_open():
                if not ib.isConnected():
                    try:
                        ib.connect(IB_IP, IB_PORT, clientId=CLIENT_ID, timeout=15)
                        ib.reqMarketDataType(3)
                        log(f"IB připojeno (MarketDataType: 3-Delayed). NY={now_ny.strftime('%H:%M:%S')}")
                        if alert_sent:
                            send_telegram_msg("IB Gateway připojena")
                            alert_sent = False

                        if not did_startup_dump:
                            dump_positions(ib, "STARTUP")
                            did_startup_dump = True

                    except Exception as e:
                        log(f"Chyba spojení s IB: {e}", "ERROR")
                        if not alert_sent:
                            send_telegram_msg(f"CRITICAL: Chyba spojení s IB Gateway - {e}")
                            alert_sent = True

                        try:
                            now_ny2 = get_ny_time()
                            write_status(
                                market_open=True,
                                ib_connected=False,
                                ny_time=now_ny2,
                                secs_to_open=0,
                                next_sell=next_sell_run_time(now_ny2),
                                next_buy=next_buy_run_time(now_ny2),
                                equity=None,
                                cash=None,
                                positions_count=None,
                                last_action="IB_CONNECT_FAILED",
                            )
                        except Exception:
                            pass

                        countdown_sleep(60, "Retry za:")
                        continue

                state = load_state()
                last_sell_id = state.get("last_sell_cycle_id", "")
                last_buy_id = state.get("last_buy_cycle_id", "")

                account_cash, portfolio_equity = read_account_summary(ib)
                if portfolio_equity <= 0:
                    log("Equity je 0 nebo se nepodařilo načíst. Přeskakuji.", "ERROR")
                    countdown_sleep(60, "Sleep:")
                    continue

                portfolio_data_latest = []

                # =========================================================
                # SELL (FIXED): bez minute%5 gate + save_state až po SELL
                # =========================================================
                if in_sell_window(now_ny):
                    sell_id = sell_cycle_id_5min(now_ny)

                    # FIX 1: pouze bucket gating přes sell_id
                    if sell_id != last_sell_id:
                        log(f"SELL-CYCLE {sell_id} | NY={now_ny.strftime('%H:%M:%S')}")

                        # provést SELL
                        changed, portfolio_data_latest = manage_positions_sell_only(conn, ib)

                        # FIX 2: save_state až po dokončení cyklu (a bez výjimky)
                        state["last_sell_cycle_id"] = sell_id
                        save_state(state)

                        if changed:
                            ib.sleep(2)
                        account_cash, portfolio_equity = read_account_summary(ib)

                        if not portfolio_data_latest:
                            ib.reqPositions()
                            ib.sleep(0.5)
                            for pos in ib.positions():
                                cp = get_current_price(pos.contract.symbol, pos.contract, ib) or pos.avgCost
                                pp = (cp - pos.avgCost) / pos.avgCost if pos.avgCost else 0
                                portfolio_data_latest.append({
                                    'symbol': pos.contract.symbol,
                                    'qty': pos.position,
                                    'avgCost': pos.avgCost,
                                    'marketPrice': cp,
                                    'pnl_pct': pp
                                })

                        now_ny2 = get_ny_time()
                        next_sell = next_sell_run_time(now_ny2)
                        next_buy = next_buy_run_time(now_ny2)

                        write_status(
                            market_open=True,
                            ib_connected=ib.isConnected(),
                            ny_time=now_ny2,
                            secs_to_open=0,
                            next_sell=next_sell,
                            next_buy=next_buy,
                            equity=portfolio_equity,
                            cash=account_cash,
                            positions_count=len(portfolio_data_latest),
                            last_action=f"SELL_CYCLE {sell_id}",
                        )

                        write_v2_snapshots(conn, portfolio_data_latest)

                # BUY větev: refresh now_ny, aby SELL→BUY ve stejné iteraci
                # neztratil otevřené okno kvůli stale snapshotu z začátku iterace
                # (např. probuzení v :30:59 → SELL fire → BUY check by jinak proběhl
                # se stale časem :30:59 a okno minute 31–33 by se minulo).
                now_ny = get_ny_time()
                if in_buy_window(now_ny):
                    buy_id = buy_cycle_id_hour(now_ny)
                    if buy_id != last_buy_id:
                        state["last_buy_cycle_id"] = buy_id
                        save_state(state)

                        log(f"BUY-CYCLE {buy_id} | NY={now_ny.strftime('%H:%M:%S')} | MODE {DIP_MODE}")
                        changed, candidates = scan_and_buy(conn, ib, account_cash, portfolio_equity)

                        if candidates:
                            LAST_CANDIDATES_REPORT = candidates

                        if changed:
                            ib.sleep(2)
                        account_cash, portfolio_equity = read_account_summary(ib)

                        portfolio_data_latest = []
                        ib.reqPositions()
                        ib.sleep(0.5)
                        for pos in ib.positions():
                            cp = get_current_price(pos.contract.symbol, pos.contract, ib) or pos.avgCost
                            pp = (cp - pos.avgCost) / pos.avgCost if pos.avgCost else 0
                            portfolio_data_latest.append({
                                'symbol': pos.contract.symbol,
                                'qty': pos.position,
                                'avgCost': pos.avgCost,
                                'marketPrice': cp,
                                'pnl_pct': pp
                            })

                        now_ny2 = get_ny_time()
                        next_sell = next_sell_run_time(now_ny2)
                        next_buy = next_buy_run_time(now_ny2)

                        write_status(
                            market_open=True,
                            ib_connected=ib.isConnected(),
                            ny_time=now_ny2,
                            secs_to_open=0,
                            next_sell=next_sell,
                            next_buy=next_buy,
                            equity=portfolio_equity,
                            cash=account_cash,
                            positions_count=len(portfolio_data_latest),
                            last_action=f"BUY_CYCLE {buy_id}",
                        )

                        write_v2_snapshots(conn, portfolio_data_latest, candidates=LAST_CANDIDATES_REPORT)

                # průběžně: refresh dashboard i bez BUY/SELL
                try:
                    refresh_dashboard(conn, ib, last_action="PERIODIC_REFRESH_OPEN")
                except Exception as e:
                    log(f"Dashboard refresh error (open): {e}", "WARNING")

                now_ny = get_ny_time()
                next_sell = next_sell_run_time(now_ny)
                next_buy = next_buy_run_time(now_ny)
                next_wake = min(next_sell, next_buy)

                # +2s buffer, aby se bot probudil UVNITŘ okna (např. :31:01),
                # ne těsně před ním (:30:59) kvůli undershootu time.sleep().
                wait = max(10, seconds_until(next_wake) + 2)
                log(f"NY {now_ny.strftime('%H:%M:%S')} | next SELL {next_sell.strftime('%H:%M:%S')} | next BUY {next_buy.strftime('%H:%M:%S')} | sleep {wait}s")
                countdown_sleep(wait, "Sleep:")

            else:
                did_startup_dump = False

                total_wait = seconds_until_market_open()
                chunk = max(60, int(DASH_REFRESH_CLOSED_SEC))

                while total_wait > 0:
                    try:
                        refresh_dashboard(conn, ib, last_action="PERIODIC_REFRESH_CLOSED")
                        log("Dashboard refresh (trh zavřený).")
                    except Exception as e:
                        log(f"Dashboard refresh error (market closed): {e}", "ERROR")
                        try:
                            if ib.isConnected():
                                ib.disconnect()
                        except Exception:
                            pass

                    sleep_now = min(chunk, total_wait)
                    next_wake_ny = get_ny_time() + datetime.timedelta(seconds=sleep_now)
                    log(f"Trh zavřený. Další open za {fmt_hms(total_wait)} | NY {next_wake_ny.strftime('%Y-%m-%d %H:%M:%S')}")
                    countdown_sleep(sleep_now, "Sleep:")
                    total_wait -= sleep_now

        except KeyboardInterrupt:
            try:
                if ib.isConnected():
                    ib.disconnect()
            except Exception:
                pass
            log("Ukončeno uživatelem.")
            break
        except Exception as e:
            log(f"CRASH: {e}", "ERROR")
            try:
                now_ny2 = get_ny_time()
                market_open = is_market_open()
                write_status(
                    market_open=market_open,
                    ib_connected=ib.isConnected() if 'ib' in locals() else False,
                    ny_time=now_ny2,
                    secs_to_open=(0 if market_open else seconds_until_market_open()),
                    next_sell=next_sell_run_time(now_ny2),
                    next_buy=next_buy_run_time(now_ny2),
                    equity=None,
                    cash=None,
                    positions_count=None,
                    last_action="CRASH",
                )
            except Exception:
                pass

            countdown_sleep(60, "Restart za:")

if __name__ == "__main__":
    main_loop()
