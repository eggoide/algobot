"""
Backtest engine - simulates trading over historical data.
"""

import datetime
from typing import Dict, List, Optional, Any, Tuple

import numpy as np
import pandas as pd

from strategy import Strategy, Signal
from backtest.portfolio import Portfolio
from backtest.metrics import calculate_metrics
from backtest.data_loader import slice_data_at_bar


class BacktestEngine:
    """
    Event-driven backtesting engine.
    Iterates over bars, checks exits first, then entries (same as live bot).
    """

    def __init__(
        self,
        strategy: Strategy,
        initial_capital: float = 10000,
        max_positions: int = 5,
        fee_per_trade: float = 1.0,
    ):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.fee = fee_per_trade
        self.portfolio = Portfolio(initial_capital, max_positions, fee_per_trade)

        # Results
        self.log_entries: List[str] = []

    def _log(self, msg: str):
        self.log_entries.append(msg)

    def run(
        self,
        data: Dict[str, pd.DataFrame],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        benchmark_symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Run backtest on historical data.

        Args:
            data: Dict of symbol -> DataFrame with OHLCV data (DatetimeIndex)
            start_date: ISO date string to start from (optional)
            end_date: ISO date string to end at (optional)
            benchmark_symbol: Symbol to use as buy-and-hold benchmark

        Returns: dict with metrics, trades, equity_curve, log
        """
        self.portfolio.reset()
        self.log_entries.clear()
        if hasattr(self.strategy, 'reset'):
            self.strategy.reset()

        # Build unified timeline from all symbols
        all_timestamps = set()
        for sym, df in data.items():
            all_timestamps.update(df.index.tolist())

        all_timestamps = sorted(all_timestamps)

        # Filter by date range
        if start_date:
            start_dt = pd.Timestamp(start_date)
            all_timestamps = [t for t in all_timestamps if t >= start_dt]
        if end_date:
            end_dt = pd.Timestamp(end_date)
            all_timestamps = [t for t in all_timestamps if t <= end_dt]

        if not all_timestamps:
            self._log("No data in date range")
            return self._build_results()

        self._log(f"Backtest: {len(all_timestamps)} bars, {len(data)} symbols")
        self._log(f"Period: {all_timestamps[0]} -> {all_timestamps[-1]}")
        self._log(f"Strategy: {self.strategy.name}")
        self._log(f"Capital: ${self.initial_capital:,.0f}, Max positions: {self.max_positions}")
        self._log("-" * 50)

        # Pre-compute bar indices for each symbol
        sym_bar_indices: Dict[str, Dict] = {}
        for sym, df in data.items():
            sym_bar_indices[sym] = {ts: i for i, ts in enumerate(df.index)}

        # Benchmark tracking
        benchmark_equity = []
        benchmark_start_price = None
        if benchmark_symbol and benchmark_symbol in data:
            bench_df = data[benchmark_symbol]

        # Main loop
        for bar_num, timestamp in enumerate(all_timestamps):
            # Get current prices for all symbols at this timestamp
            current_prices: Dict[str, float] = {}
            for sym, df in data.items():
                if timestamp in df.index:
                    current_prices[sym] = float(df.loc[timestamp, 'Close'])

            if not current_prices:
                continue

            # 1. Check exits first
            symbols_to_exit = list(self.portfolio.positions.keys())
            for sym in symbols_to_exit:
                pos = self.portfolio.positions.get(sym)
                if pos is None:
                    continue

                price = current_prices.get(sym)
                if price is None:
                    continue

                holding_bars = bar_num - pos.entry_bar

                # Get data slice for this symbol up to current bar
                sym_data = None
                if sym in data:
                    sym_df = data[sym]
                    mask = sym_df.index <= timestamp
                    sym_data = sym_df[mask]

                exit_signal = self.strategy.should_exit(
                    sym, pos.entry_price, price, holding_bars, sym_data
                )
                if exit_signal:
                    ts_str = timestamp.strftime('%Y-%m-%d %H:%M') if hasattr(timestamp, 'strftime') else str(timestamp)
                    trade = self.portfolio.sell(sym, price, bar_num, timestamp, exit_signal.reason)
                    if trade:
                        self._log(
                            f"SELL {sym} @ ${price:.2f} | PnL ${trade.pnl:+.2f} | "
                            f"{exit_signal.reason} | held {trade.holding_bars} bars | {ts_str}"
                        )

            # 2. Check entries
            if self.portfolio.can_buy:
                # Build data slices up to current bar (prevent look-ahead)
                data_slice = {}
                for sym, df in data.items():
                    mask = df.index <= timestamp
                    sliced = df[mask]
                    if not sliced.empty:
                        data_slice[sym] = sliced

                existing = list(self.portfolio.positions.keys())
                signals = self.strategy.generate_signals(data_slice, existing)

                for signal in signals:
                    if not self.portfolio.can_buy:
                        break
                    if signal.symbol not in current_prices:
                        continue

                    price = current_prices[signal.symbol]
                    trade = self.portfolio.buy(
                        signal.symbol, price, bar_num, timestamp, signal.reason
                    )
                    if trade:
                        ts_str = timestamp.strftime('%Y-%m-%d %H:%M') if hasattr(timestamp, 'strftime') else str(timestamp)
                        self._log(
                            f"BUY  {signal.symbol} @ ${price:.2f} | qty={trade.qty} | "
                            f"{signal.reason} | {ts_str}"
                        )

            # 3. Record equity
            self.portfolio.record_equity(current_prices)

            # 4. Benchmark
            if benchmark_symbol and benchmark_symbol in current_prices:
                bp = current_prices[benchmark_symbol]
                if benchmark_start_price is None:
                    benchmark_start_price = bp
                bench_value = self.initial_capital * (bp / benchmark_start_price)
                benchmark_equity.append(bench_value)

            # Progress
            if bar_num > 0 and bar_num % 500 == 0:
                eq = self.portfolio.equity_curve[-1] if self.portfolio.equity_curve else self.initial_capital
                self._log(f"  ... bar {bar_num}/{len(all_timestamps)} | equity ${eq:,.0f}")

        # Close remaining positions at last available prices
        last_prices = {}
        for sym, df in data.items():
            if not df.empty:
                last_prices[sym] = float(df['Close'].iloc[-1])

        remaining = list(self.portfolio.positions.keys())
        for sym in remaining:
            price = last_prices.get(sym)
            if price:
                trade = self.portfolio.sell(sym, price, len(all_timestamps) - 1, all_timestamps[-1], "End of Backtest")
                if trade:
                    self._log(f"CLOSE {sym} @ ${price:.2f} | PnL ${trade.pnl:+.2f} | End of Backtest")

        self.portfolio.record_equity(last_prices)

        return self._build_results(benchmark_equity if benchmark_equity else None)

    def _build_results(self, benchmark_equity: Optional[List[float]] = None) -> Dict[str, Any]:
        metrics = calculate_metrics(self.portfolio, benchmark_equity)
        return {
            "metrics": metrics,
            "trades": self.portfolio.trades,
            "equity_curve": self.portfolio.equity_curve,
            "benchmark_equity": benchmark_equity,
            "log": self.log_entries,
        }


class ParameterOptimizer:
    """
    Grid search optimizer for strategy parameters.
    Supports walk-forward analysis.
    """

    def __init__(
        self,
        strategy_class,
        base_params: Dict[str, Any],
        initial_capital: float = 10000,
        max_positions: int = 5,
        fee: float = 1.0,
    ):
        self.strategy_class = strategy_class
        self.base_params = base_params
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.fee = fee

    def grid_search(
        self,
        data: Dict[str, pd.DataFrame],
        param_grid: Dict[str, List],
        optimize_metric: str = "sharpe_ratio",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Run backtest for each combination of parameters.

        Args:
            data: Historical data
            param_grid: e.g. {"buy_drop": [0.01, 0.02, 0.03], "rsi_limit": [25, 30, 35]}
            optimize_metric: Metric to optimize (e.g., "sharpe_ratio", "total_return_pct")

        Returns: List of results sorted by optimize_metric (best first)
        """
        import itertools

        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combinations = list(itertools.product(*values))

        print(f"Grid search: {len(combinations)} combinations")
        results = []

        for i, combo in enumerate(combinations):
            params = dict(self.base_params)
            combo_dict = dict(zip(keys, combo))
            params.update(combo_dict)

            strategy = self.strategy_class(params)
            engine = BacktestEngine(strategy, self.initial_capital, self.max_positions, self.fee)
            result = engine.run(data, start_date, end_date)

            entry = {
                "params": combo_dict,
                "metrics": result["metrics"],
                optimize_metric: result["metrics"].get(optimize_metric, 0),
            }
            results.append(entry)

            metric_val = result["metrics"].get(optimize_metric, 0)
            print(f"  [{i+1}/{len(combinations)}] {combo_dict} -> {optimize_metric}={metric_val:.4f}")

        results.sort(key=lambda x: x[optimize_metric], reverse=True)
        return results

    def walk_forward(
        self,
        data: Dict[str, pd.DataFrame],
        param_grid: Dict[str, List],
        train_bars: int,
        test_bars: int,
        optimize_metric: str = "sharpe_ratio",
    ) -> Dict[str, Any]:
        """
        Walk-forward optimization:
        1. Optimize on train_bars window
        2. Test on next test_bars window
        3. Slide forward and repeat

        Returns combined out-of-sample results.
        """
        # Build unified timeline
        all_timestamps = set()
        for df in data.values():
            all_timestamps.update(df.index.tolist())
        all_timestamps = sorted(all_timestamps)

        total_bars = len(all_timestamps)
        if total_bars < train_bars + test_bars:
            print("Not enough data for walk-forward")
            return {"windows": [], "combined_metrics": {}}

        windows = []
        pos = 0

        while pos + train_bars + test_bars <= total_bars:
            train_start = all_timestamps[pos]
            train_end = all_timestamps[pos + train_bars - 1]
            test_start = all_timestamps[pos + train_bars]
            test_end = all_timestamps[min(pos + train_bars + test_bars - 1, total_bars - 1)]

            print(f"\n--- Window {len(windows)+1}: Train {train_start} -> {train_end} | Test {test_start} -> {test_end}")

            # Optimize on train period
            best = self.grid_search(
                data, param_grid, optimize_metric,
                start_date=str(train_start), end_date=str(train_end)
            )

            if not best:
                pos += test_bars
                continue

            best_params = best[0]["params"]
            print(f"  Best params: {best_params}")

            # Test on out-of-sample period
            full_params = dict(self.base_params)
            full_params.update(best_params)
            strategy = self.strategy_class(full_params)
            engine = BacktestEngine(strategy, self.initial_capital, self.max_positions, self.fee)
            result = engine.run(data, str(test_start), str(test_end))

            windows.append({
                "train_start": str(train_start),
                "train_end": str(train_end),
                "test_start": str(test_start),
                "test_end": str(test_end),
                "best_params": best_params,
                "oos_metrics": result["metrics"],
            })

            pos += test_bars

        return {"windows": windows}
