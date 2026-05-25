"""Backtest job execution — wraps the existing BacktestEngine for the web UI.

Two job kinds:
- "single": straight backtest with given params/symbols/period.
- "replay": load live trades from /data/algobot.db, run backtest over the same
  window/symbols/params, return both series for divergence analysis.
"""

from __future__ import annotations

import os
import sqlite3
import datetime
from typing import Any, Dict, List, Optional

import yaml

from backtest.data_loader import download_bulk, get_sp100_tickers
from backtest.engine import BacktestEngine
from backtest.metrics import calculate_metrics
from strategy import DipBuyStrategy, EnhancedDipBuyStrategy


CONFIG_PATH = "/app/config.yaml"
DB_PATH = os.environ.get("DB_PATH", "/data/algobot.db")
CACHE_DIR = os.environ.get("BACKTEST_CACHE_DIR", "/data/backtest_cache")


def load_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f) or {}
    return {}


def build_strategy_params(cfg: Dict[str, Any], strategy_name: str, overrides: Dict[str, Any]) -> Dict[str, Any]:
    strat = cfg.get("strategy", {})
    params = {
        "dip_mode": strat.get("dip_mode", "DAILY"),
        "buy_drop": strat.get("buy_drop", 0.02),
        "sell_gain": strat.get("sell_gain", 0.03),
        "rsi_limit": strat.get("rsi_limit", 30),
        "rsi_period": strat.get("rsi_period", 14),
        "use_stop_loss": strat.get("use_stop_loss", False),
        "stop_loss": strat.get("stop_loss", 0.15),
        "use_sma_filter": strat.get("use_sma_filter", False),
        "sma_period": strat.get("sma_period", 200),
    }
    if strategy_name == "enhanced":
        params.update({
            "use_stop_loss": strat.get("use_stop_loss", True),
            "stop_loss": strat.get("stop_loss", 0.07),
            "use_trailing_stop": strat.get("use_trailing_stop", True),
            "trailing_stop_pct": strat.get("trailing_stop_pct", 0.02),
            "use_time_stop": strat.get("use_time_stop", True),
            "time_stop_bars": strat.get("time_stop_bars", 240),
            "use_macd": strat.get("use_macd", True),
            "macd_fast": strat.get("macd_fast", 12),
            "macd_slow": strat.get("macd_slow", 26),
            "macd_signal": strat.get("macd_signal", 9),
            "use_bollinger": strat.get("use_bollinger", True),
            "bb_period": strat.get("bb_period", 20),
            "bb_std": strat.get("bb_std", 2.0),
            "use_volume_filter": strat.get("use_volume_filter", False),
            "volume_multiplier": strat.get("volume_multiplier", 1.5),
            "rsi_limit": strat.get("rsi_limit", 35),
        })
    # Apply UI overrides last
    for k, v in (overrides or {}).items():
        if v is not None:
            params[k] = v
    return params


def _serialize_trades(trades: list) -> List[Dict[str, Any]]:
    out = []
    for t in trades:
        out.append({
            "ts": t.timestamp.isoformat() if hasattr(t.timestamp, "isoformat") and t.timestamp else None,
            "action": t.action,
            "symbol": t.symbol,
            "price": float(t.price),
            "qty": int(t.qty),
            "pnl": float(t.pnl),
            "fee": float(t.fee),
            "reason": t.reason or "",
            "holding_bars": int(t.holding_bars),
        })
    return out


def _serialize_equity(equity_curve: list, timestamps: Optional[list] = None) -> List[Dict[str, Any]]:
    out = []
    for i, v in enumerate(equity_curve):
        if timestamps and i < len(timestamps):
            ts = timestamps[i]
            ts_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        else:
            ts_iso = None
        out.append({"i": i, "ts": ts_iso, "equity": float(v)})
    return out


def run_single(job) -> Dict[str, Any]:
    """job.params: {strategy, symbols, period, interval, start_date, end_date,
                    slippage_pct, fee_model, use_next_open, auto_adjust,
                    capital, max_positions, benchmark, strategy_overrides}
    """
    p = job.params
    cfg = load_config()
    capital_cfg = cfg.get("capital", {})

    initial_capital = float(p.get("capital") or capital_cfg.get("manual_capital_limit", 10000))
    max_positions = int(p.get("max_positions") or capital_cfg.get("max_positions", 5))
    fee_per_trade = float(capital_cfg.get("fee_usd", 1.0))

    bt_cfg = cfg.get("backtest", {})
    slippage_pct = float(p.get("slippage_pct") if p.get("slippage_pct") is not None else bt_cfg.get("slippage_pct", 0.0005))
    fee_model = p.get("fee_model") or bt_cfg.get("fee_model", "ib")
    use_next_open = bool(p.get("use_next_open") if p.get("use_next_open") is not None else bt_cfg.get("use_next_open", True))
    auto_adjust = bool(p.get("auto_adjust") if p.get("auto_adjust") is not None else bt_cfg.get("auto_adjust", True))

    strategy_name = p.get("strategy", "enhanced")
    if p.get("symbols"):
        symbols = [s.strip().upper() for s in p["symbols"].split(",") if s.strip()]
    else:
        symbols = get_sp100_tickers()

    job.progress = f"Stahuji data: {len(symbols)} symbolů ({p.get('period','2y')}, {p.get('interval','1h')})..."

    os.makedirs(CACHE_DIR, exist_ok=True)
    data = download_bulk(
        symbols,
        period=p.get("period", "2y"),
        interval=p.get("interval", "1h"),
        cache_dir=CACHE_DIR,
        force_refresh=bool(p.get("refresh", False)),
        max_cache_age_hours=int(p.get("cache_hours", 24)),
        auto_adjust=auto_adjust,
    )
    if not data:
        raise RuntimeError("Nepodařilo se stáhnout žádná data")

    job.progress = f"Backtest: {len(data)} symbolů, strategy={strategy_name}..."

    params = build_strategy_params(cfg, strategy_name, p.get("strategy_overrides") or {})
    strategy = EnhancedDipBuyStrategy(params) if strategy_name == "enhanced" else DipBuyStrategy(params)

    engine = BacktestEngine(
        strategy, initial_capital, max_positions,
        fee_per_trade=fee_per_trade,
        slippage_pct=slippage_pct,
        fee_model=fee_model,
        use_next_open=use_next_open,
    )

    results = engine.run(
        data,
        start_date=p.get("start_date"),
        end_date=p.get("end_date"),
        benchmark_symbol=p.get("benchmark") or "SPY",
    )

    # Build timestamps for equity curve (use union of all symbol indices, filtered to engine range)
    import pandas as pd
    all_ts = sorted({ts for df in data.values() for ts in df.index})
    sample_tz = None
    if all_ts:
        sample_tz = getattr(all_ts[0], "tz", None) or getattr(all_ts[0], "tzinfo", None)

    def _norm(ts_str):
        ts = pd.Timestamp(ts_str)
        if sample_tz is not None:
            ts = ts.tz_localize(sample_tz) if ts.tzinfo is None else ts.tz_convert(sample_tz)
        elif ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        return ts

    if p.get("start_date"):
        all_ts = [t for t in all_ts if t >= _norm(p["start_date"])]
    if p.get("end_date"):
        all_ts = [t for t in all_ts if t <= _norm(p["end_date"])]

    return {
        "strategy_name": strategy.name,
        "params": params,
        "config": {
            "initial_capital": initial_capital,
            "max_positions": max_positions,
            "fee_per_trade": fee_per_trade,
            "slippage_pct": slippage_pct,
            "fee_model": fee_model,
            "use_next_open": use_next_open,
            "auto_adjust": auto_adjust,
            "period": p.get("period", "2y"),
            "interval": p.get("interval", "1h"),
            "start_date": p.get("start_date"),
            "end_date": p.get("end_date"),
            "symbols": list(data.keys()),
            "benchmark": p.get("benchmark") or "SPY",
        },
        "metrics": results["metrics"],
        "trades": _serialize_trades(results["trades"]),
        "equity_curve": _serialize_equity(results["equity_curve"], all_ts),
        "benchmark_equity": results.get("benchmark_equity"),
        "log_tail": results["log"][-200:],
    }


def _load_live_trades(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ts, action, symbol, qty, price, pnl, note FROM trades ORDER BY ts"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def run_replay(job) -> Dict[str, Any]:
    """Run backtest on the same period/symbols as live trade history. Compare side-by-side."""
    p = job.params
    live = _load_live_trades()
    if not live:
        raise RuntimeError("Žádné live obchody v algobot.db")

    # Determine window from live trades
    live_buys = [t for t in live if t["action"] == "BUY"]
    if not live_buys:
        raise RuntimeError("Žádný BUY v live historii — nelze replay-ovat")

    first_buy = min(t["ts"] for t in live_buys)
    last_act = max(t["ts"] for t in live)
    start_dt = datetime.datetime.fromisoformat(first_buy).date()
    end_dt = datetime.datetime.fromisoformat(last_act).date() + datetime.timedelta(days=1)

    symbols = sorted(set(t["symbol"] for t in live))

    # Compute realized PnL from live trades (only SELLs with fill)
    live_realized = sum(float(t["pnl"]) for t in live if t["action"] == "SELL")

    # Run backtest with same window + same symbols + current config params
    bt_params = dict(p)
    bt_params["symbols"] = ",".join(symbols)
    bt_params["start_date"] = str(start_dt)
    bt_params["end_date"] = str(end_dt)
    bt_params.setdefault("period", "2y")  # need enough history for indicators on start_date
    bt_params.setdefault("interval", "1h")
    bt_params.setdefault("strategy", "enhanced")

    # Inject a tiny shim job so run_single can use job.progress
    class _Shim:
        progress = ""
        params = bt_params
    shim = _Shim()
    bt_result = run_single(shim)

    bt_sells = [t for t in bt_result["trades"] if t["action"] == "SELL"]
    bt_realized = sum(t["pnl"] for t in bt_sells)

    # Per-symbol divergence
    from collections import defaultdict
    sym_live = defaultdict(lambda: {"buys": 0, "sells": 0, "realized": 0.0})
    sym_bt = defaultdict(lambda: {"buys": 0, "sells": 0, "realized": 0.0})
    for t in live:
        s = sym_live[t["symbol"]]
        if t["action"] == "BUY":
            s["buys"] += 1
        else:
            s["sells"] += 1
            s["realized"] += float(t["pnl"])
    for t in bt_result["trades"]:
        s = sym_bt[t["symbol"]]
        if t["action"] == "BUY":
            s["buys"] += 1
        else:
            s["sells"] += 1
            s["realized"] += float(t["pnl"])

    rows = []
    for sym in sorted(set(list(sym_live.keys()) + list(sym_bt.keys()))):
        l = sym_live.get(sym, {"buys": 0, "sells": 0, "realized": 0.0})
        b = sym_bt.get(sym, {"buys": 0, "sells": 0, "realized": 0.0})
        rows.append({
            "symbol": sym,
            "live_buys": l["buys"], "live_sells": l["sells"], "live_realized": l["realized"],
            "bt_buys": b["buys"], "bt_sells": b["sells"], "bt_realized": b["realized"],
            "diff": b["realized"] - l["realized"],
        })

    return {
        "window": {"start": str(start_dt), "end": str(end_dt), "symbols": symbols},
        "live": {"trades": live, "realized": live_realized, "trade_count": len(live)},
        "backtest": bt_result,
        "per_symbol": rows,
        "summary": {
            "live_realized": live_realized,
            "bt_realized": bt_realized,
            "diff": bt_realized - live_realized,
        },
    }
