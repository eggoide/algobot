"""
Performance metrics for backtesting.
"""

import math
from typing import List, Dict, Any, Optional

import numpy as np

from backtest.portfolio import Portfolio, Trade


def calculate_metrics(portfolio: Portfolio, benchmark_equity: Optional[List[float]] = None) -> Dict[str, Any]:
    """Calculate comprehensive performance metrics from a completed backtest."""
    sell_trades = portfolio.get_sell_trades()
    all_trades = portfolio.trades
    equity_curve = portfolio.equity_curve

    metrics: Dict[str, Any] = {}

    # Basic
    metrics["initial_capital"] = portfolio.initial_cash
    final_equity = equity_curve[-1] if equity_curve else portfolio.initial_cash
    metrics["final_equity"] = final_equity
    metrics["total_return"] = final_equity - portfolio.initial_cash
    metrics["total_return_pct"] = ((final_equity / portfolio.initial_cash) - 1) * 100 if portfolio.initial_cash > 0 else 0

    # Trade counts
    metrics["total_trades"] = len(all_trades)
    metrics["buy_trades"] = len(portfolio.get_buy_trades())
    metrics["sell_trades"] = len(sell_trades)

    # Win/Loss
    if sell_trades:
        winning = [t for t in sell_trades if t.pnl > 0]
        losing = [t for t in sell_trades if t.pnl < 0]
        breakeven = [t for t in sell_trades if t.pnl == 0]

        metrics["winning_trades"] = len(winning)
        metrics["losing_trades"] = len(losing)
        metrics["breakeven_trades"] = len(breakeven)
        metrics["win_rate"] = len(winning) / len(sell_trades) * 100

        pnls = [t.pnl for t in sell_trades]
        metrics["total_pnl"] = sum(pnls)
        metrics["avg_pnl"] = np.mean(pnls)
        metrics["median_pnl"] = np.median(pnls)
        metrics["best_trade"] = max(pnls)
        metrics["worst_trade"] = min(pnls)
        metrics["avg_winning_trade"] = np.mean([t.pnl for t in winning]) if winning else 0
        metrics["avg_losing_trade"] = np.mean([t.pnl for t in losing]) if losing else 0

        # Profit Factor
        gross_profit = sum(t.pnl for t in winning)
        gross_loss = abs(sum(t.pnl for t in losing))
        metrics["gross_profit"] = gross_profit
        metrics["gross_loss"] = gross_loss
        metrics["profit_factor"] = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        # Holding period
        holding_bars = [t.holding_bars for t in sell_trades if t.holding_bars > 0]
        if holding_bars:
            metrics["avg_holding_bars"] = np.mean(holding_bars)
            metrics["max_holding_bars"] = max(holding_bars)
            metrics["min_holding_bars"] = min(holding_bars)

        # Total fees
        metrics["total_fees"] = sum(t.fee for t in all_trades)

        # Exit reasons
        reasons: Dict[str, int] = {}
        for t in sell_trades:
            r = t.reason or "Unknown"
            reasons[r] = reasons.get(r, 0) + 1
        metrics["exit_reasons"] = reasons
    else:
        metrics["winning_trades"] = 0
        metrics["losing_trades"] = 0
        metrics["win_rate"] = 0
        metrics["total_pnl"] = 0
        metrics["profit_factor"] = 0

    # Equity curve metrics
    if len(equity_curve) > 1:
        eq = np.array(equity_curve)

        # Max Drawdown
        peak = np.maximum.accumulate(eq)
        drawdown = (eq - peak) / peak
        metrics["max_drawdown_pct"] = float(np.min(drawdown) * 100)
        metrics["max_drawdown_abs"] = float(np.min(eq - peak))

        # Sharpe Ratio (annualized, assuming hourly bars)
        returns = np.diff(eq) / eq[:-1]
        if len(returns) > 1 and np.std(returns) > 0:
            # Assume ~1950 trading hours per year (252 days * ~7.75 hours)
            annualization = math.sqrt(1950)
            metrics["sharpe_ratio"] = float(np.mean(returns) / np.std(returns) * annualization)
        else:
            metrics["sharpe_ratio"] = 0.0

        # Sortino Ratio (only downside deviation)
        if len(returns) > 1:
            downside = returns[returns < 0]
            if len(downside) > 0 and np.std(downside) > 0:
                annualization = math.sqrt(1950)
                metrics["sortino_ratio"] = float(np.mean(returns) / np.std(downside) * annualization)
            else:
                metrics["sortino_ratio"] = float('inf') if np.mean(returns) > 0 else 0.0
        else:
            metrics["sortino_ratio"] = 0.0

        # Calmar Ratio
        max_dd = abs(metrics["max_drawdown_pct"])
        if max_dd > 0:
            metrics["calmar_ratio"] = metrics["total_return_pct"] / max_dd
        else:
            metrics["calmar_ratio"] = float('inf') if metrics["total_return_pct"] > 0 else 0.0

    else:
        metrics["max_drawdown_pct"] = 0
        metrics["max_drawdown_abs"] = 0
        metrics["sharpe_ratio"] = 0
        metrics["sortino_ratio"] = 0
        metrics["calmar_ratio"] = 0

    # Benchmark comparison
    if benchmark_equity and len(benchmark_equity) > 1:
        bench_return = ((benchmark_equity[-1] / benchmark_equity[0]) - 1) * 100
        metrics["benchmark_return_pct"] = bench_return
        metrics["alpha"] = metrics["total_return_pct"] - bench_return
    else:
        metrics["benchmark_return_pct"] = None
        metrics["alpha"] = None

    return metrics


def format_metrics(metrics: Dict[str, Any]) -> str:
    """Format metrics as a readable text report."""
    lines = []
    lines.append("=" * 60)
    lines.append("BACKTEST RESULTS")
    lines.append("=" * 60)

    lines.append(f"\n--- Capital ---")
    lines.append(f"  Initial:       ${metrics['initial_capital']:,.2f}")
    lines.append(f"  Final:         ${metrics['final_equity']:,.2f}")
    lines.append(f"  Total Return:  ${metrics['total_return']:,.2f} ({metrics['total_return_pct']:+.2f}%)")

    lines.append(f"\n--- Trades ---")
    lines.append(f"  Total:         {metrics['total_trades']}")
    lines.append(f"  Buys:          {metrics['buy_trades']}")
    lines.append(f"  Sells:         {metrics['sell_trades']}")

    if metrics['sell_trades'] > 0:
        lines.append(f"  Winning:       {metrics['winning_trades']}")
        lines.append(f"  Losing:        {metrics['losing_trades']}")
        lines.append(f"  Win Rate:      {metrics['win_rate']:.1f}%")

        lines.append(f"\n--- P&L ---")
        lines.append(f"  Total PnL:     ${metrics['total_pnl']:,.2f}")
        lines.append(f"  Avg PnL:       ${metrics['avg_pnl']:,.2f}")
        lines.append(f"  Best Trade:    ${metrics['best_trade']:,.2f}")
        lines.append(f"  Worst Trade:   ${metrics['worst_trade']:,.2f}")
        lines.append(f"  Profit Factor: {metrics['profit_factor']:.2f}")
        lines.append(f"  Total Fees:    ${metrics.get('total_fees', 0):,.2f}")

        if 'avg_holding_bars' in metrics:
            lines.append(f"\n--- Holding Period ---")
            lines.append(f"  Avg:           {metrics['avg_holding_bars']:.0f} bars")
            lines.append(f"  Max:           {metrics['max_holding_bars']} bars")
            lines.append(f"  Min:           {metrics['min_holding_bars']} bars")

        if 'exit_reasons' in metrics:
            lines.append(f"\n--- Exit Reasons ---")
            for reason, count in sorted(metrics['exit_reasons'].items(), key=lambda x: -x[1]):
                lines.append(f"  {reason}: {count}")

    lines.append(f"\n--- Risk Metrics ---")
    lines.append(f"  Max Drawdown:  {metrics['max_drawdown_pct']:.2f}%")
    lines.append(f"  Sharpe Ratio:  {metrics['sharpe_ratio']:.2f}")
    lines.append(f"  Sortino Ratio: {metrics['sortino_ratio']:.2f}")
    lines.append(f"  Calmar Ratio:  {metrics['calmar_ratio']:.2f}")

    if metrics.get('benchmark_return_pct') is not None:
        lines.append(f"\n--- Benchmark ---")
        lines.append(f"  Benchmark:     {metrics['benchmark_return_pct']:+.2f}%")
        lines.append(f"  Alpha:         {metrics['alpha']:+.2f}%")

    lines.append("=" * 60)
    return "\n".join(lines)
