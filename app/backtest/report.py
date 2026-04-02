"""
HTML report generator for backtest results.
"""

import json
import os
from typing import Dict, Any, List, Optional


def generate_html_report(
    results: Dict[str, Any],
    strategy_name: str = "",
    strategy_params: Dict[str, Any] = None,
    output_path: str = "backtest_report.html",
) -> str:
    """Generate a standalone HTML report for backtest results."""
    metrics = results["metrics"]
    trades = results["trades"]
    equity_curve = results["equity_curve"]
    benchmark_equity = results.get("benchmark_equity")
    log_entries = results.get("log", [])

    # Prepare chart data
    equity_js = json.dumps(equity_curve[-2000:] if len(equity_curve) > 2000 else equity_curve)
    bench_js = json.dumps(benchmark_equity[-2000:] if benchmark_equity and len(benchmark_equity) > 2000 else (benchmark_equity or []))
    labels_js = json.dumps(list(range(len(equity_curve[-2000:]))))

    # Trade table rows
    trade_rows = ""
    for t in trades:
        pnl_cls = "win" if t.pnl > 0 else ("loss" if t.pnl < 0 else "neutral")
        action_cls = "buy" if t.action == "BUY" else "sell"
        ts_str = t.timestamp.strftime('%Y-%m-%d %H:%M') if t.timestamp else ""
        trade_rows += f"""<tr>
            <td>{ts_str}</td>
            <td><span class="badge {action_cls}">{t.action}</span></td>
            <td>{t.symbol}</td>
            <td>{t.qty}</td>
            <td>${t.price:.2f}</td>
            <td class="{pnl_cls}">${t.pnl:.2f}</td>
            <td>{t.reason}</td>
            <td>{t.holding_bars}</td>
        </tr>"""

    # Params display
    params_html = ""
    if strategy_params:
        for k, v in strategy_params.items():
            params_html += f"<div><span class='param-key'>{k}:</span> <span class='param-val'>{v}</span></div>"

    # Exit reasons chart data
    exit_reasons = metrics.get("exit_reasons", {})
    exit_labels_js = json.dumps(list(exit_reasons.keys()))
    exit_values_js = json.dumps(list(exit_reasons.values()))

    # Log
    log_text = "\n".join(log_entries[-500:])

    total_return_style = "color:#3fb950;" if metrics['total_return'] >= 0 else "color:#f85149;"
    win_rate = metrics.get('win_rate', 0)
    win_rate_style = "color:#3fb950;" if win_rate >= 50 else "color:#f85149;"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Backtest Report - {strategy_name}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {{ --bg: #0f1116; --panel: #161b22; --border: #30363d; --text: #c9d1d9; --accent: #58a6ff; }}
        body {{ font-family: 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; font-size: 14px; }}
        h1 {{ color: var(--accent); font-size: 1.5rem; margin-bottom: 5px; }}
        h2 {{ font-size: 1rem; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 15px; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 20px; }}
        .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }}
        .stat {{ font-size: 1.8rem; font-weight: 600; color: #fff; }}
        .label {{ color: #8b949e; font-size: 0.85rem; margin-top: 4px; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        th {{ text-align: left; color: #8b949e; padding: 8px 5px; border-bottom: 1px solid var(--border); }}
        td {{ padding: 8px 5px; border-bottom: 1px solid #21262d; }}
        .badge {{ padding: 2px 8px; border-radius: 12px; font-size: 0.75rem; font-weight: 600; }}
        .badge.buy {{ background: rgba(35,134,54,0.2); color: #3fb950; }}
        .badge.sell {{ background: rgba(218,54,51,0.2); color: #f85149; }}
        .win {{ color: #3fb950; font-weight: 600; }}
        .loss {{ color: #f85149; font-weight: 600; }}
        .neutral {{ color: #8b949e; }}
        .param-key {{ color: #8b949e; }}
        .param-val {{ color: #fff; font-weight: 600; }}
        pre {{ background: rgba(0,0,0,0.3); border: 1px solid var(--border); border-radius: 8px; padding: 12px;
               font-size: 12px; overflow: auto; max-height: 400px; white-space: pre-wrap; }}
        .metric-row {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #21262d; }}
        .metric-row:last-child {{ border-bottom: none; }}
    </style>
</head>
<body>
    <h1>Backtest Report</h1>
    <div style="color:#8b949e; margin-bottom:20px;">
        Strategy: <strong style="color:#fff">{strategy_name}</strong>
        | Bars: {len(equity_curve)}
        | Trades: {metrics['total_trades']}
    </div>

    <div class="grid">
        <div class="card">
            <h2>Return</h2>
            <div class="stat" style="{total_return_style}">${metrics['total_return']:,.2f}</div>
            <div class="label">{metrics['total_return_pct']:+.2f}% total return</div>
            <div style="margin-top:10px;">
                <div class="metric-row"><span>Initial</span><span>${metrics['initial_capital']:,.0f}</span></div>
                <div class="metric-row"><span>Final</span><span>${metrics['final_equity']:,.0f}</span></div>
            </div>
        </div>

        <div class="card">
            <h2>Win Rate</h2>
            <div class="stat" style="{win_rate_style}">{win_rate:.1f}%</div>
            <div class="label">{metrics.get('winning_trades',0)}W / {metrics.get('losing_trades',0)}L / {metrics.get('breakeven_trades',0)}BE</div>
            <div style="margin-top:10px;">
                <div class="metric-row"><span>Avg Win</span><span class="win">${metrics.get('avg_winning_trade',0):,.2f}</span></div>
                <div class="metric-row"><span>Avg Loss</span><span class="loss">${metrics.get('avg_losing_trade',0):,.2f}</span></div>
                <div class="metric-row"><span>Profit Factor</span><span>{metrics.get('profit_factor',0):.2f}</span></div>
            </div>
        </div>

        <div class="card">
            <h2>Risk</h2>
            <div class="stat" style="color:#f85149;">{metrics.get('max_drawdown_pct',0):.2f}%</div>
            <div class="label">Max Drawdown</div>
            <div style="margin-top:10px;">
                <div class="metric-row"><span>Sharpe</span><span>{metrics.get('sharpe_ratio',0):.2f}</span></div>
                <div class="metric-row"><span>Sortino</span><span>{metrics.get('sortino_ratio',0):.2f}</span></div>
                <div class="metric-row"><span>Calmar</span><span>{metrics.get('calmar_ratio',0):.2f}</span></div>
            </div>
        </div>

        <div class="card">
            <h2>Trade Stats</h2>
            <div style="margin-top:5px;">
                <div class="metric-row"><span>Best Trade</span><span class="win">${metrics.get('best_trade',0):,.2f}</span></div>
                <div class="metric-row"><span>Worst Trade</span><span class="loss">${metrics.get('worst_trade',0):,.2f}</span></div>
                <div class="metric-row"><span>Avg PnL</span><span>${metrics.get('avg_pnl',0):,.2f}</span></div>
                <div class="metric-row"><span>Avg Hold</span><span>{metrics.get('avg_holding_bars',0):.0f} bars</span></div>
                <div class="metric-row"><span>Total Fees</span><span>${metrics.get('total_fees',0):,.2f}</span></div>
            </div>
        </div>
    </div>

    {"<div class='grid'><div class='card'><h2>Benchmark</h2><div class='metric-row'><span>Strategy</span><span>" + f"{metrics['total_return_pct']:+.2f}%" + "</span></div><div class='metric-row'><span>Benchmark</span><span>" + f"{metrics.get('benchmark_return_pct',0):+.2f}%" + "</span></div><div class='metric-row'><span>Alpha</span><span style='color:" + ('#3fb950' if (metrics.get('alpha') or 0) >= 0 else '#f85149') + "'>" + f"{metrics.get('alpha',0):+.2f}%" + "</span></div></div></div>" if metrics.get('benchmark_return_pct') is not None else ""}

    <div class="card" style="margin-bottom:20px;">
        <h2>Equity Curve</h2>
        <div style="height: 300px;"><canvas id="eqChart"></canvas></div>
    </div>

    <div class="grid" style="grid-template-columns: 2fr 1fr;">
        <div class="card">
            <h2>Trades ({len(trades)})</h2>
            <div style="max-height:500px; overflow:auto;">
            <table>
                <thead><tr><th>Date</th><th>Action</th><th>Symbol</th><th>Qty</th><th>Price</th><th>PnL</th><th>Reason</th><th>Bars</th></tr></thead>
                <tbody>{trade_rows}</tbody>
            </table>
            </div>
        </div>

        <div class="card">
            <h2>Parameters</h2>
            <div style="margin-top:5px;">{params_html}</div>

            <h2 style="margin-top:20px;">Exit Reasons</h2>
            <div style="height:200px;"><canvas id="exitChart"></canvas></div>
        </div>
    </div>

    <div class="card" style="margin-top:20px;">
        <h2>Log</h2>
        <pre>{log_text}</pre>
    </div>

    <div style="text-align:center; color:#484f58; margin-top:30px; font-size:0.8rem;">AlgoBot Backtest Report</div>

    <script>
        // Equity curve
        const eqCtx = document.getElementById('eqChart').getContext('2d');
        const datasets = [{{
            label: 'Strategy',
            data: {equity_js},
            borderColor: '#58a6ff',
            backgroundColor: 'rgba(88,166,255,0.08)',
            borderWidth: 2, pointRadius: 0, fill: true, tension: 0.3
        }}];

        const benchData = {bench_js};
        if (benchData.length > 0) {{
            datasets.push({{
                label: 'Benchmark (Buy & Hold)',
                data: benchData,
                borderColor: '#8b949e',
                borderWidth: 1.5, pointRadius: 0, fill: false,
                borderDash: [5, 5], tension: 0.3
            }});
        }}

        new Chart(eqCtx, {{
            type: 'line',
            data: {{ labels: {labels_js}, datasets: datasets }},
            options: {{
                responsive: true, maintainAspectRatio: false,
                plugins: {{ legend: {{ labels: {{ color: '#8b949e' }} }} }},
                scales: {{
                    x: {{ display: false }},
                    y: {{ grid: {{ color: '#21262d' }}, ticks: {{ color: '#8b949e', callback: v => '$' + v.toLocaleString() }} }}
                }}
            }}
        }});

        // Exit reasons pie
        const exitCtx = document.getElementById('exitChart').getContext('2d');
        const exitLabels = {exit_labels_js};
        const exitValues = {exit_values_js};
        if (exitLabels.length > 0) {{
            new Chart(exitCtx, {{
                type: 'doughnut',
                data: {{
                    labels: exitLabels,
                    datasets: [{{ data: exitValues, backgroundColor: ['#3fb950','#f85149','#d29922','#58a6ff','#8b949e','#bc8cff'] }}]
                }},
                options: {{
                    responsive: true, maintainAspectRatio: false,
                    plugins: {{ legend: {{ position: 'bottom', labels: {{ color: '#8b949e', font: {{ size: 11 }} }} }} }}
                }}
            }});
        }}
    </script>
</body>
</html>"""

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    return output_path
