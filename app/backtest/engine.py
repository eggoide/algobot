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

    Execution model:
    - Signals are evaluated on the CLOSE of bar N (same data the live bot would see
      after that bar completes).
    - Orders fill at the OPEN of bar N+1 (use_next_open=True, default) — no look-ahead.
    - Slippage and per-share IB fees are applied by Portfolio.
    - Legacy mode: use_next_open=False fills at signal-bar Close (has look-ahead bias).
    """

    def __init__(
        self,
        strategy: Strategy,
        initial_capital: float = 10000,
        max_positions: int = 5,
        fee_per_trade: float = 1.0,
        slippage_pct: float = 0.0,
        fee_model: str = "flat",
        use_next_open: bool = True,
    ):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.fee = fee_per_trade
        self.slippage_pct = slippage_pct
        self.fee_model = fee_model
        self.use_next_open = use_next_open
        self.portfolio = Portfolio(
            initial_capital, max_positions, fee_per_trade,
            slippage_pct=slippage_pct, fee_model=fee_model,
        )

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

        # Filter by date range — normalize tz so naive user input matches tz-aware data
        sample_tz = None
        if all_timestamps:
            first = all_timestamps[0]
            sample_tz = getattr(first, "tz", None) or getattr(first, "tzinfo", None)

        def _to_match_tz(ts_str):
            ts = pd.Timestamp(ts_str)
            if sample_tz is not None:
                ts = ts.tz_localize(sample_tz) if ts.tzinfo is None else ts.tz_convert(sample_tz)
            elif ts.tzinfo is not None:
                ts = ts.tz_localize(None)
            return ts

        if start_date:
            start_dt = _to_match_tz(start_date)
            all_timestamps = [t for t in all_timestamps if t >= start_dt]
        if end_date:
            end_dt = _to_match_tz(end_date)
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

        # Pending orders to execute at NEXT bar's Open (no look-ahead)
        # Each entry: {"action": "BUY"|"SELL", "symbol": ..., "reason": ..., "signal_bar": ...}
        pending_orders: List[Dict[str, Any]] = []

        # Main loop
        for bar_num, timestamp in enumerate(all_timestamps):
            # Get prices for all symbols at this timestamp (Close for decisions, Open for fills)
            current_close: Dict[str, float] = {}
            current_open: Dict[str, float] = {}
            for sym, df in data.items():
                if timestamp in df.index:
                    row = df.loc[timestamp]
                    current_close[sym] = float(row['Close'])
                    current_open[sym] = float(row['Open']) if 'Open' in df.columns else float(row['Close'])

            if not current_close:
                continue

            # 0. Execute pending orders from previous bar's signals (at THIS bar's Open)
            if self.use_next_open and pending_orders:
                still_pending = []
                for order in pending_orders:
                    sym = order["symbol"]
                    fill_price = current_open.get(sym)
                    if fill_price is None:
                        # Symbol has no data this bar — defer once more or drop
                        if order.get("retries", 0) < 1:
                            order["retries"] = order.get("retries", 0) + 1
                            still_pending.append(order)
                        continue

                    ts_str = timestamp.strftime('%Y-%m-%d %H:%M') if hasattr(timestamp, 'strftime') else str(timestamp)
                    if order["action"] == "SELL":
                        pos = self.portfolio.positions.get(sym)
                        if pos is None:
                            continue
                        trade = self.portfolio.sell(sym, fill_price, bar_num, timestamp, order["reason"])
                        if trade:
                            self._log(
                                f"SELL {sym} @ ${trade.price:.2f} | PnL ${trade.pnl:+.2f} | "
                                f"{order['reason']} | held {trade.holding_bars} bars | {ts_str}"
                            )
                    elif order["action"] == "BUY":
                        if sym in self.portfolio.positions or not self.portfolio.can_buy:
                            continue
                        trade = self.portfolio.buy(sym, fill_price, bar_num, timestamp, order["reason"])
                        if trade:
                            self._log(
                                f"BUY  {sym} @ ${trade.price:.2f} | qty={trade.qty} | "
                                f"{order['reason']} | {ts_str}"
                            )
                pending_orders = still_pending

            # 1. Evaluate exit signals (against this bar's Close)
            symbols_to_exit = list(self.portfolio.positions.keys())
            for sym in symbols_to_exit:
                pos = self.portfolio.positions.get(sym)
                if pos is None:
                    continue

                price = current_close.get(sym)
                if price is None:
                    continue

                holding_bars = bar_num - pos.entry_bar

                sym_data = None
                if sym in data:
                    sym_df = data[sym]
                    mask = sym_df.index <= timestamp
                    sym_data = sym_df[mask]

                exit_signal = self.strategy.should_exit(
                    sym, pos.entry_price, price, holding_bars, sym_data
                )
                if exit_signal:
                    if self.use_next_open:
                        # Queue for fill at next bar Open
                        pending_orders.append({
                            "action": "SELL", "symbol": sym,
                            "reason": exit_signal.reason, "signal_bar": bar_num,
                        })
                    else:
                        # Legacy: fill at signal-bar Close (has look-ahead)
                        ts_str = timestamp.strftime('%Y-%m-%d %H:%M') if hasattr(timestamp, 'strftime') else str(timestamp)
                        trade = self.portfolio.sell(sym, price, bar_num, timestamp, exit_signal.reason)
                        if trade:
                            self._log(
                                f"SELL {sym} @ ${trade.price:.2f} | PnL ${trade.pnl:+.2f} | "
                                f"{exit_signal.reason} | held {trade.holding_bars} bars | {ts_str}"
                            )

            # 2. Evaluate entry signals (against this bar's Close)
            # Reserve slots for queued BUYs so we don't oversubscribe
            queued_buys = sum(1 for o in pending_orders if o["action"] == "BUY")
            if self.portfolio.can_buy and (self.portfolio.open_position_count + queued_buys < self.max_positions):
                data_slice = {}
                for sym, df in data.items():
                    mask = df.index <= timestamp
                    sliced = df[mask]
                    if not sliced.empty:
                        data_slice[sym] = sliced

                existing = list(self.portfolio.positions.keys()) + [o["symbol"] for o in pending_orders if o["action"] == "BUY"]
                signals = self.strategy.generate_signals(data_slice, existing)

                slots_left = self.max_positions - self.portfolio.open_position_count - queued_buys
                for signal in signals:
                    if slots_left <= 0:
                        break
                    if signal.symbol not in current_close:
                        continue

                    if self.use_next_open:
                        pending_orders.append({
                            "action": "BUY", "symbol": signal.symbol,
                            "reason": signal.reason, "signal_bar": bar_num,
                        })
                        slots_left -= 1
                    else:
                        price = current_close[signal.symbol]
                        trade = self.portfolio.buy(
                            signal.symbol, price, bar_num, timestamp, signal.reason
                        )
                        if trade:
                            ts_str = timestamp.strftime('%Y-%m-%d %H:%M') if hasattr(timestamp, 'strftime') else str(timestamp)
                            self._log(
                                f"BUY  {signal.symbol} @ ${trade.price:.2f} | qty={trade.qty} | "
                                f"{signal.reason} | {ts_str}"
                            )
                            slots_left -= 1

            # 3. Record equity (mark-to-market at this bar's Close)
            self.portfolio.record_equity(current_close)

            # 4. Benchmark
            if benchmark_symbol and benchmark_symbol in current_close:
                bp = current_close[benchmark_symbol]
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
        slippage_pct: float = 0.0,
        fee_model: str = "flat",
        use_next_open: bool = True,
    ):
        self.strategy_class = strategy_class
        self.base_params = base_params
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.fee = fee
        self.slippage_pct = slippage_pct
        self.fee_model = fee_model
        self.use_next_open = use_next_open

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
            engine = BacktestEngine(
                strategy, self.initial_capital, self.max_positions, self.fee,
                slippage_pct=self.slippage_pct, fee_model=self.fee_model,
                use_next_open=self.use_next_open,
            )
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
            engine = BacktestEngine(
                strategy, self.initial_capital, self.max_positions, self.fee,
                slippage_pct=self.slippage_pct, fee_model=self.fee_model,
                use_next_open=self.use_next_open,
            )
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
