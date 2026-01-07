import datetime
import json
import os
import sys
import time
from io import StringIO
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
import pytz
import requests
import yaml
import yfinance as yf
from ib_insync import IB, MarketOrder, Stock

from db import db_connect, insert_trade, last_trades, cumulative_pnl_series

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
REPORT_FILE = os.getenv("REPORT_FILE", "/reports/index.html")

TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# =========================================================
# PARAMS (from config.yaml)
# =========================================================
MANUAL_CAPITAL_LIMIT = float(CAPITAL.get("manual_capital_limit", 10000))
MAX_POSITIONS = int(CAPITAL.get("max_positions", 5))
FEE = float(CAPITAL.get("fee_usd", 1.0))

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

LAST_CANDIDATES_REPORT: List[Dict[str, Any]] = []
SMA_CACHE: Dict[str, Tuple[datetime.datetime, bool]] = {}

# =========================================================
# LOGGING
# =========================================================
def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [{level}] {msg}", flush=True)

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

def is_market_open() -> bool:
    ny_now = get_ny_time()
    if ny_now.weekday() > 4:
        return False
    market_open = ny_now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = ny_now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= ny_now <= market_close

def seconds_until_market_open() -> int:
    ny_now = get_ny_time()
    today_open = ny_now.replace(hour=9, minute=30, second=0, microsecond=0)

    if ny_now < today_open:
        return max(60, int((today_open - ny_now).total_seconds()))

    next_day = ny_now + datetime.timedelta(days=1)
    while next_day.weekday() > 4:
        next_day += datetime.timedelta(days=1)

    next_open = next_day.replace(hour=9, minute=30, second=0, microsecond=0)
    return max(60, int((next_open - ny_now).total_seconds()))

def seconds_until(dt_target: datetime.datetime) -> int:
    now = get_ny_time()
    return max(1, int((dt_target - now).total_seconds()))

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

    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    log(f"{prefix} {h:02d}:{m:02d}:{s:02d}")
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
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi

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
# REPORT
# =========================================================
def generate_html_report(conn, equity, portfolio, candidates, cash):
    history_rows = ""
    try:
        hist = last_trades(conn, TRADES_TABLE_LIMIT)
        for row in hist:
            pnl_val = float(row["pnl"])
            cls = "win" if pnl_val > 0 else ("loss" if pnl_val < 0 else "neutral")
            action_cls = "buy" if row["action"] == 'BUY' else "sell"
            history_rows += (
                f"""<tr><td>{row['ts'].replace('T',' ')}</td><td><span class="badge {action_cls}">{row['action']}</span></td>"""
                f"""<td>{row['symbol']}</td><td>{row['qty']}</td><td>${float(row['price']):.2f}</td>"""
                f"""<td class="{cls}">{pnl_val:.2f}</td></tr>"""
            )
    except Exception:
        pass

    portfolio_rows = ""
    if portfolio:
        for p in portfolio:
            pnl_cls = "win" if p['pnl_pct'] >= 0 else "loss"
            portfolio_rows += (
                f"""<tr><td><strong>{p['symbol']}</strong></td><td>{p['qty']}</td>"""
                f"""<td>${p['avgCost']:.2f}</td><td>${p['marketPrice']:.2f}</td>"""
                f"""<td><span class="badge {pnl_cls}">{p['pnl_pct']*100:.2f}%</span></td></tr>"""
            )
    else:
        portfolio_rows = """<tr><td colspan="5" style="text-align:center; color:#555; padding: 20px;">Žádné otevřené pozice</td></tr>"""

    candidates_rows = ""
    if candidates:
        sorted_candidates = sorted(candidates, key=lambda x: x['rsi'])[:CANDIDATES_LIMIT]
        for c in sorted_candidates:
            rsi_val = float(c['rsi'])
            rsi_style = "color: #da3633; font-weight:bold;" if rsi_val <= RSI_LIMIT else ("color: #d29922;" if rsi_val < 40 else "color: #8b949e;")

            drop_val = float(c['drop'])
            drop_cls = "loss" if drop_val <= -BUY_DROP else "neutral"

            is_buy = c.get('is_buy_signal', False)
            if is_buy:
                status_badge = '<span class="badge buy">NÁKUP</span>'
                row_style = "background: rgba(35, 134, 54, 0.1);"
            else:
                status_badge = '<span style="color:#8b949e; font-size:0.8em;">Sledovat</span>'
                row_style = ""

            sma_info = ""
            if USE_SMA_FILTER:
                sma_info = '<span style="font-size:0.8em; color:#238636;"> ✓SMA</span>' if c.get('sma_ok') else '<span style="font-size:0.8em; color:#da3633;"> ✕SMA</span>'

            candidates_rows += (
                f"""<tr style="{row_style}">"""
                f"""<td><strong>{c['symbol']}</strong>{sma_info}</td>"""
                f"""<td style="{rsi_style}">{rsi_val:.1f}</td>"""
                f"""<td class="{drop_cls}">{drop_val*100:.2f}%</td>"""
                f"""<td>{status_badge}</td></tr>"""
            )
    else:
        candidates_rows = """<tr><td colspan="4" style="text-align:center; color:#555;">Žádná data ke zobrazení</td></tr>"""

    chart_dates, chart_vals = cumulative_pnl_series(conn)

    total_pnl = float(chart_vals[-1]) if chart_vals else 0.0
    capital_base = float(MANUAL_CAPITAL_LIMIT if MANUAL_CAPITAL_LIMIT else equity)
    capital_base = capital_base if capital_base > 0 else 1.0

    roi_pct = (total_pnl / capital_base) * 100.0
    roi_str = f"{roi_pct:+.2f}%"
    total_pnl_style = "color:#3fb950;" if total_pnl >= 0 else "color:#f85149;"

    chart_dates_js = json.dumps(chart_dates)
    chart_vals_js = json.dumps(chart_vals)

    sl_text = f"SL {STOP_LOSS*100:.0f}%" if USE_STOP_LOSS else "NO SL"

    html = f"""
    <!DOCTYPE html>
    <html lang="cs">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AlgoBot Dashboard</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600&display=swap" rel="stylesheet">
        <style>
            :root {{
                --bg: #0f1116; --panel: #161b22; --border: #30363d;
                --text: #c9d1d9; --accent: #58a6ff;
            }}
            body {{ font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; font-size: 14px; }}
            .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 15px; }}
            h1 {{ margin: 0; font-weight: 600; color: var(--accent); font-size: 1.5rem; }}
            h2 {{ font-size: 1rem; margin-bottom: 15px; color: #8b949e; font-weight: 400; text-transform: uppercase; letter-spacing: 1px; }}
            .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin-bottom: 20px; }}
            .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.2); }}
            .stat-box {{ display: flex; justify-content: space-between; align-items: baseline; }}
            .stat-val {{ font-size: 2rem; font-weight: 600; color: #fff; }}
            table {{ width: 100%; border-collapse: collapse; }}
            th {{ text-align: left; color: #8b949e; padding: 10px 5px; border-bottom: 1px solid var(--border); font-size: 0.85rem; }}
            td {{ padding: 12px 5px; border-bottom: 1px solid #21262d; }}
            tr:last-child td {{ border-bottom: none; }}
            .badge {{ padding: 3px 8px; border-radius: 12px; font-size: 0.75rem; font-weight: 600; }}
            .badge.buy {{ background: rgba(35, 134, 54, 0.2); color: #3fb950; border: 1px solid rgba(35, 134, 54, 0.5); }}
            .badge.sell {{ background: rgba(218, 54, 51, 0.2); color: #f85149; border: 1px solid rgba(218, 54, 51, 0.5); }}
            .win {{ color: #3fb950; font-weight: 600; }}
            .loss {{ color: #f85149; font-weight: 600; }}
            .neutral {{ color: #c9d1d9; }}
            .footer {{ text-align: center; margin-top: 40px; color: #484f58; font-size: 0.8rem; }}
        </style>
    </head>
    <body>
        <div class="header">
            <div>
                <h1>AlgoBot <span style="font-weight:300; color:#8b949e;">Dashboard</span></h1>
                <div style="color: #8b949e; font-size: 0.85rem; margin-top: 5px;">
                    Mode: <span style="color:#fff">{DIP_MODE}</span> | Buy -{BUY_DROP*100:.0f}% / Sell +{SELL_GAIN*100:.0f}% / RSI &lt; {RSI_LIMIT} / {sl_text}
                </div>
            </div>
            <div style="text-align: right;">
                <div style="font-weight: 600; color: #fff;">{datetime.datetime.now().strftime("%d.%m. %H:%M")}</div>
                <div style="color: #484f58;">Poslední aktualizace</div>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <h2>Celková Equity</h2>
                <div class="stat-box">
                    <div class="stat-val">${equity:,.0f}</div>
                </div>
                <div style="margin-top: 10px; font-size: 0.9rem; color: #8b949e;">
                    Hotovost: <span style="color:#fff">${cash:,.0f}</span>
                </div>
            </div>

            <div class="card">
                <h2>Alokace Portfolia</h2>
                <div class="stat-box">
                    <div class="stat-val">{len(portfolio)} <span style="font-size:1.2rem; color:#8b949e;">/ {MAX_POSITIONS}</span></div>
                </div>
                <div style="width: 100%; background: #21262d; height: 6px; border-radius: 3px; margin-top: 15px; overflow:hidden;">
                    <div style="width: {(len(portfolio)/MAX_POSITIONS)*100 if MAX_POSITIONS else 0}%; background: var(--accent); height: 100%;"></div>
                </div>
            </div>

            <div class="card">
                <h2>Vývoj Zisku</h2>

                <div style="display:flex; justify-content:space-between; align-items:baseline; margin-bottom:10px;">
                    <div style="display:flex; align-items:baseline; gap:10px;">
                        <div style="font-size: 1.6rem; font-weight: 600; {total_pnl_style}">
                            ${total_pnl:,.2f}
                        </div>
                        <div style="font-size: 0.95rem; font-weight: 600; {total_pnl_style}">
                            ({roi_str})
                        </div>
                    </div>
                    <div style="color:#8b949e; font-size:0.85rem;">
                        Celkem PnL / ROI
                    </div>
                </div>

                <div style="height: 100px;">
                    <canvas id="pnlChart"></canvas>
                </div>
            </div>
        </div>

        <div class="grid" style="grid-template-columns: 1.5fr 1fr;">
            <div class="card">
                <h2>Otevřené Pozice</h2>
                <table>
                    <thead><tr><th>Symbol</th><th>Ks</th><th>Nákup</th><th>Cena</th><th>P/L</th></tr></thead>
                    <tbody>{portfolio_rows}</tbody>
                </table>
            </div>

            <div class="card">
                <h2>Watchlist (Nejnižší RSI)</h2>
                <table>
                    <thead><tr><th>Symbol</th><th>RSI</th><th>Drop</th><th>Stav</th></tr></thead>
                    <tbody>{candidates_rows}</tbody>
                </table>
            </div>
        </div>

        <div class="card">
            <h2>Poslední Obchody</h2>
            <table>
                <thead><tr><th>Datum</th><th>Akce</th><th>Symbol</th><th>Ks</th><th>Cena</th><th>Zisk</th></tr></thead>
                <tbody>{history_rows}</tbody>
            </table>
        </div>

        <div class="footer">AlgoBot 2026</div>

        <script>
            const ctx = document.getElementById('pnlChart').getContext('2d');
            new Chart(ctx, {{
                type: 'line',
                data: {{
                    labels: {chart_dates_js},
                    datasets: [{{
                        data: {chart_vals_js},
                        borderColor: '#58a6ff',
                        backgroundColor: 'rgba(88, 166, 255, 0.1)',
                        borderWidth: 2,
                        pointRadius: 0,
                        fill: true,
                        tension: 0.4
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{ legend: {{ display: false }} }},
                    scales: {{
                        x: {{ display: false }},
                        y: {{ grid: {{ color: '#21262d' }}, ticks: {{ color: '#8b949e' }} }}
                    }}
                }}
            }});
        </script>
    </body>
    </html>
    """
    try:
        with open(REPORT_FILE, "w", encoding='utf-8') as f:
            f.write(html)
        log(f"REPORT aktualizován: {REPORT_FILE}")
    except Exception as e:
        log(f"REPORT ERROR: {e}", "ERROR")

# =========================================================
# CORE: SELL
# =========================================================
def manage_positions_sell_only(conn, ib: IB):
    ib.reqPositions()
    ib.sleep(0.5)
    current_positions = ib.positions()

    positions_changed = False
    portfolio_data = []

    log(f"SELL-CHECK: pozic {len(current_positions)}")

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
        action = None
        reason = ""

        if pnl_pct >= SELL_GAIN:
            action, reason = "SELL", "Take Profit"
        elif USE_STOP_LOSS and pnl_pct <= -STOP_LOSS:
            action, reason = "SELL", "Stop Loss"

        log(f"{contract.symbol} PnL {pnl_pct*100:+.2f}% (Cena {curr_price:.2f})")

        if action == "SELL":
            try:
                if any(o.contract.symbol == contract.symbol and o.order.action == 'SELL' for o in ib.openOrders()):
                    log(f"{contract.symbol} SELL už existuje v openOrders, skip", "WARNING")
                    portfolio_data.append({
                        'symbol': contract.symbol,
                        'qty': pos.position,
                        'avgCost': pos.avgCost,
                        'marketPrice': curr_price,
                        'pnl_pct': pnl_pct
                    })
                    continue
            except Exception:
                pass

            sell_contract = Stock(contract.symbol, 'SMART', 'USD')
            order = MarketOrder('SELL', pos.position, tif='DAY')
            ib.placeOrder(sell_contract, order)

            ib.sleep(1)
            realized = (curr_price - pos.avgCost) * pos.position - FEE
            log_trade(conn, 'SELL', contract.symbol, curr_price, int(pos.position), float(realized), reason)
            send_telegram_msg(f"SELL {contract.symbol} ({reason}) PnL ${realized:.2f}")
            positions_changed = True
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

    ib.reqPositions()
    ib.sleep(0.5)
    current_positions = ib.positions()

    if len(current_positions) >= MAX_POSITIONS:
        log(f"BUY-SCAN: portfolio plné ({len(current_positions)}/{MAX_POSITIONS})")
        return False, candidates_report

    tickers = get_sp100_tickers()
    log(f"BUY-SCAN: stahuji data pro {len(tickers)} tickerů")

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

        potential_buys = []
        for t in tickers:
            try:
                df = data[t] if isinstance(data.columns, pd.MultiIndex) else data
                df = df.dropna(subset=['Close'])
                if len(df) < RSI_PERIOD + 3:
                    continue

                curr = float(df['Close'].iloc[-1])

                if DIP_MODE == "DAILY":
                    last_bar_date = df.index[-1].date()
                    prev_days_df = df[df.index.date < last_bar_date]
                    reference_price = float(prev_days_df['Close'].iloc[-1]) if not prev_days_df.empty else float(df['Close'].iloc[-2])
                else:
                    reference_price = float(df['Close'].iloc[-2])

                if not reference_price:
                    continue

                drop = (curr - reference_price) / reference_price
                rsi_val = float(calculate_rsi_wilder(df['Close'], RSI_PERIOD).iloc[-1])
                if np.isnan(rsi_val):
                    continue

                sma_passed = True
                if USE_SMA_FILTER:
                    sma_passed = check_daily_sma200(t, SMA_PERIOD)

                is_signal = (drop <= -BUY_DROP and rsi_val < RSI_LIMIT and sma_passed)

                if rsi_val < SHOW_CANDIDATES_RSI_BELOW:
                    candidates_report.append({
                        'symbol': t,
                        'price': curr,
                        'rsi': rsi_val,
                        'drop': drop,
                        'is_buy_signal': is_signal,
                        'sma_ok': sma_passed
                    })

                if is_signal:
                    potential_buys.append({'symbol': t, 'price': curr, 'rsi': rsi_val, 'drop': drop})

            except Exception:
                continue

        potential_buys.sort(key=lambda x: x['rsi'])

        if not potential_buys:
            log(f"BUY-SCAN: žádný signál (kandidátů do reportu {len(candidates_report)})")
            return False, candidates_report

        top = potential_buys[0]
        ib_sym = yf_to_ib_symbol(top['symbol'])

        has_pos = any(p.contract.symbol == ib_sym for p in current_positions)
        try:
            has_ord = any(o.contract.symbol == ib_sym and o.order.action == 'BUY' for o in ib.openOrders())
        except Exception:
            has_ord = False

        if has_pos or has_ord:
            log(f"BUY-SCAN: SKIP {ib_sym} už v portfoliu nebo v openOrders", "WARNING")
            return False, candidates_report

        qty = int((position_size_usd / top['price']) // 1)
        est_cost = qty * top['price']

        if qty <= 0:
            log("BUY-SCAN: SKIP qty=0", "WARNING")
            return False, candidates_report

        if account_cash <= est_cost:
            log(f"BUY-SCAN: SKIP nedostatek hotovosti (cash ${account_cash:.0f} < est ${est_cost:.0f})", "WARNING")
            return False, candidates_report

        log(f"BUY {ib_sym} RSI {top['rsi']:.1f} drop {top['drop']*100:.2f}% qty={qty}")
        contract = Stock(ib_sym, 'SMART', 'USD')
        order = MarketOrder('BUY', qty, tif='DAY')
        ib.placeOrder(contract, order)

        note_text = f"Mode:{DIP_MODE}, Dip:{BUY_DROP*100:.0f}%"
        log_trade(conn, 'BUY', ib_sym, float(top['price']), int(qty), -FEE, note_text)
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

    log("STARTUJI ALGO-BOT (SELL každých 5 min, BUY 1x/h v 10:31–15:33 NY)")
    send_telegram_msg("AlgoBot start")

    ib = IB()
    alert_sent = False
    did_startup_dump = False

    while True:
        try:
            now_ny = get_ny_time()

            if is_market_open():
                if not ib.isConnected():
                    try:
                        ib.connect(IB_IP, IB_PORT, clientId=CLIENT_ID)
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

                if in_sell_window(now_ny):
                    sell_id = sell_cycle_id_5min(now_ny)
                    if sell_id != last_sell_id and (now_ny.minute % 5 == 0 or now_ny.minute % 5 == 1):
                        state["last_sell_cycle_id"] = sell_id
                        save_state(state)

                        log(f"SELL-CYCLE {sell_id} | NY={now_ny.strftime('%H:%M:%S')}")
                        changed, portfolio_data_latest = manage_positions_sell_only(conn, ib)

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

                        generate_html_report(conn, portfolio_equity, portfolio_data_latest, LAST_CANDIDATES_REPORT, account_cash)

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

                        generate_html_report(conn, portfolio_equity, portfolio_data_latest, LAST_CANDIDATES_REPORT, account_cash)

                now_ny = get_ny_time()
                next_sell = next_sell_run_time(now_ny)
                next_buy = next_buy_run_time(now_ny)
                next_wake = min(next_sell, next_buy)

                wait = max(10, seconds_until(next_wake))
                log(f"NY {now_ny.strftime('%H:%M:%S')} | next SELL {next_sell.strftime('%H:%M:%S')} | next BUY {next_buy.strftime('%H:%M:%S')} | sleep {wait}s")
                countdown_sleep(wait, "Sleep:")

            else:
                if ib.isConnected():
                    ib.disconnect()
                    log("IB odpojeno (trh zavřený).")
                did_startup_dump = False

                wait = seconds_until_market_open()
                next_wake_ny = get_ny_time() + datetime.timedelta(seconds=wait)
                log(f"Trh zavřený. Další open za {wait//3600:02d}:{(wait%3600)//60:02d} | NY {next_wake_ny.strftime('%Y-%m-%d %H:%M:%S')}")
                countdown_sleep(wait, "Sleep:")

        except KeyboardInterrupt:
            if ib.isConnected():
                ib.disconnect()
            log("Ukončeno uživatelem.")
            break
        except Exception as e:
            log(f"CRASH: {e}", "ERROR")
            countdown_sleep(60, "Restart za:")

if __name__ == "__main__":
    main_loop()

