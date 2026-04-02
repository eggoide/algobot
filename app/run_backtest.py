#!/usr/bin/env python3
"""
AlgoBot Backtesting CLI.

Usage:
    python run_backtest.py                          # Run with defaults (DipBuy strategy)
    python run_backtest.py --strategy enhanced      # Run with Enhanced strategy
    python run_backtest.py --optimize               # Grid search optimization
    python run_backtest.py --walk-forward            # Walk-forward analysis
    python run_backtest.py --period 1y --interval 1d # Custom data period
    python run_backtest.py --symbols AAPL,MSFT,NVDA  # Custom symbols
"""

import argparse
import os
import sys
import yaml

# Add app directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy import DipBuyStrategy, EnhancedDipBuyStrategy, create_strategy
from backtest.data_loader import download_bulk, download_multiple, get_sp100_tickers
from backtest.engine import BacktestEngine, ParameterOptimizer
from backtest.metrics import format_metrics
from backtest.report import generate_html_report


def load_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    return {}


def get_strategy_params(cfg: dict, strategy_name: str) -> dict:
    """Build strategy params from config + defaults."""
    strat = cfg.get("strategy", {})
    capital = cfg.get("capital", {})

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
            "time_stop_bars": strat.get("time_stop_bars", 120),
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

    return params


def run_single_backtest(args):
    """Run a single backtest."""
    cfg = load_config()
    capital_cfg = cfg.get("capital", {})

    initial_capital = args.capital or capital_cfg.get("manual_capital_limit", 10000)
    max_positions = args.max_positions or capital_cfg.get("max_positions", 5)
    fee = capital_cfg.get("fee_usd", 1.0)

    # Get symbols
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        print("Loading S&P 100 tickers...")
        symbols = get_sp100_tickers()
    print(f"Symbols: {len(symbols)}")

    # Download data
    cache_dir = os.path.join(os.path.expanduser("~"), ".algobot_cache")
    data = download_bulk(
        symbols,
        period=args.period,
        interval=args.interval,
        cache_dir=cache_dir,
        force_refresh=args.refresh,
        max_cache_age_hours=args.cache_hours,
    )

    if not data:
        print("ERROR: No data downloaded")
        return

    # Create strategy
    strategy_name = args.strategy
    params = get_strategy_params(cfg, strategy_name)

    if strategy_name == "enhanced":
        strategy = EnhancedDipBuyStrategy(params)
    else:
        strategy = DipBuyStrategy(params)

    print(f"\nStrategy: {strategy.name}")
    print(f"Capital: ${initial_capital:,}, Max positions: {max_positions}, Fee: ${fee}")
    print(f"Params: {params}")
    print()

    # Run backtest
    engine = BacktestEngine(strategy, initial_capital, max_positions, fee)
    results = engine.run(
        data,
        start_date=args.start_date,
        end_date=args.end_date,
        benchmark_symbol=args.benchmark or "AAPL",
    )

    # Print results
    print()
    print(format_metrics(results["metrics"]))

    # Generate HTML report
    report_dir = os.path.join(os.path.expanduser("~"), ".algobot_cache", "reports")
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, f"backtest_{strategy_name}.html")
    generate_html_report(results, strategy.name, params, report_path)
    print(f"\nHTML Report: {os.path.abspath(report_path)}")


def run_optimization(args):
    """Run grid search optimization."""
    cfg = load_config()
    capital_cfg = cfg.get("capital", {})

    initial_capital = args.capital or capital_cfg.get("manual_capital_limit", 10000)
    max_positions = args.max_positions or capital_cfg.get("max_positions", 5)
    fee = capital_cfg.get("fee_usd", 1.0)

    # Get symbols
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        symbols = get_sp100_tickers()

    # Download data
    cache_dir = os.path.join(os.path.expanduser("~"), ".algobot_cache")
    data = download_bulk(symbols, period=args.period, interval=args.interval, cache_dir=cache_dir)

    if not data:
        print("ERROR: No data")
        return

    # Strategy class
    if args.strategy == "enhanced":
        strategy_class = EnhancedDipBuyStrategy
    else:
        strategy_class = DipBuyStrategy

    base_params = get_strategy_params(cfg, args.strategy)

    # Parameter grid
    param_grid = {
        "buy_drop": [0.01, 0.02, 0.03, 0.04, 0.05],
        "sell_gain": [0.02, 0.03, 0.04, 0.05, 0.08],
        "rsi_limit": [25, 30, 35, 40],
    }

    if args.strategy == "enhanced":
        param_grid["stop_loss"] = [0.05, 0.07, 0.10]
        param_grid["trailing_stop_pct"] = [0.015, 0.02, 0.03]

    optimizer = ParameterOptimizer(strategy_class, base_params, initial_capital, max_positions, fee)
    results = optimizer.grid_search(
        data, param_grid,
        optimize_metric=args.optimize_metric,
        start_date=args.start_date,
        end_date=args.end_date,
    )

    print("\n" + "=" * 70)
    print("TOP 10 PARAMETER COMBINATIONS")
    print("=" * 70)
    for i, r in enumerate(results[:10]):
        m = r["metrics"]
        print(f"\n#{i+1}: {r['params']}")
        print(f"  Return: {m['total_return_pct']:+.2f}% | Sharpe: {m['sharpe_ratio']:.2f} | "
              f"Win Rate: {m.get('win_rate',0):.1f}% | MaxDD: {m['max_drawdown_pct']:.2f}% | "
              f"Trades: {m['sell_trades']}")


def run_walk_forward(args):
    """Run walk-forward analysis."""
    cfg = load_config()
    capital_cfg = cfg.get("capital", {})

    initial_capital = args.capital or capital_cfg.get("manual_capital_limit", 10000)
    max_positions = args.max_positions or capital_cfg.get("max_positions", 5)
    fee = capital_cfg.get("fee_usd", 1.0)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        symbols = get_sp100_tickers()

    cache_dir = os.path.join(os.path.expanduser("~"), ".algobot_cache")
    data = download_bulk(symbols, period=args.period, interval=args.interval, cache_dir=cache_dir)

    if not data:
        print("ERROR: No data")
        return

    if args.strategy == "enhanced":
        strategy_class = EnhancedDipBuyStrategy
    else:
        strategy_class = DipBuyStrategy

    base_params = get_strategy_params(cfg, args.strategy)

    param_grid = {
        "buy_drop": [0.01, 0.02, 0.03, 0.05],
        "sell_gain": [0.02, 0.03, 0.05],
        "rsi_limit": [25, 30, 35],
    }

    optimizer = ParameterOptimizer(strategy_class, base_params, initial_capital, max_positions, fee)
    results = optimizer.walk_forward(
        data, param_grid,
        train_bars=args.train_bars,
        test_bars=args.test_bars,
        optimize_metric=args.optimize_metric,
    )

    print("\n" + "=" * 70)
    print("WALK-FORWARD RESULTS")
    print("=" * 70)
    for i, w in enumerate(results.get("windows", [])):
        m = w["oos_metrics"]
        print(f"\nWindow {i+1}: {w['test_start']} -> {w['test_end']}")
        print(f"  Best params: {w['best_params']}")
        print(f"  OOS Return: {m['total_return_pct']:+.2f}% | Sharpe: {m['sharpe_ratio']:.2f} | "
              f"Trades: {m['sell_trades']} | Win Rate: {m.get('win_rate',0):.1f}%")


def main():
    parser = argparse.ArgumentParser(description="AlgoBot Backtester")
    parser.add_argument("--strategy", choices=["dip_buy", "enhanced"], default="dip_buy",
                        help="Strategy to backtest")
    parser.add_argument("--period", default="2y", help="Data period (1mo, 3mo, 6mo, 1y, 2y, 5y)")
    parser.add_argument("--interval", default="1h", help="Data interval (1m, 5m, 15m, 1h, 1d)")
    parser.add_argument("--symbols", default=None, help="Comma-separated symbols (default: S&P 100)")
    parser.add_argument("--capital", type=float, default=None, help="Initial capital")
    parser.add_argument("--max-positions", type=int, default=None, help="Max simultaneous positions")
    parser.add_argument("--benchmark", default="AAPL", help="Benchmark symbol for comparison")
    parser.add_argument("--start-date", default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--refresh", action="store_true", help="Force refresh cached data")
    parser.add_argument("--cache-hours", type=int, default=72, help="Max cache age in hours")

    # Optimization
    parser.add_argument("--optimize", action="store_true", help="Run grid search optimization")
    parser.add_argument("--walk-forward", action="store_true", help="Run walk-forward analysis")
    parser.add_argument("--optimize-metric", default="sharpe_ratio",
                        help="Metric to optimize (sharpe_ratio, total_return_pct, profit_factor, win_rate)")
    parser.add_argument("--train-bars", type=int, default=1000, help="Walk-forward: training window size")
    parser.add_argument("--test-bars", type=int, default=250, help="Walk-forward: test window size")

    args = parser.parse_args()

    if args.walk_forward:
        run_walk_forward(args)
    elif args.optimize:
        run_optimization(args)
    else:
        run_single_backtest(args)


if __name__ == "__main__":
    main()
