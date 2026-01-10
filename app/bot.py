import yfinance as yf
import pandas as pd
import requests
import numpy as np
from io import StringIO
import matplotlib.pyplot as plt

# =========================================================
# 1) KONFIGURACE
# =========================================================
SIM_START = "2026-01-01"
SIM_END   = "2026-01-10"      # období, které chceš vyhodnotit
DATA_START = "2025-10-01"     # warmup pro RSI (klidně 2–6 měsíců zpět)
DATA_END   = "2026-01-11"     # yfinance end je typicky exclusive -> dej o den víc

INITIAL_CAPITAL = 10000
MAX_POSITIONS = 5
FEE = 1.0

BUY_DROP = 0.02
SELL_GAIN = 0.03
USE_STOP_LOSS = False
STOP_LOSS_PCT = 0.15

RSI_PERIOD = 14
RSI_LIMIT = 30

def get_sp100_tickers():
    url = "https://en.wikipedia.org/wiki/S%26P_100"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        tables = pd.read_html(StringIO(resp.text))
        df = next(t for t in tables if 'Symbol' in t.columns)
        return [t.replace('.', '-') for t in df['Symbol'].tolist()]
    except:
        return ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'JPM', 'WMT', 'V', 'NOW']

def calculate_rsi_wilder(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    return 100 - (100 / (1 + rs))

class Backtester:
    def __init__(self):
        self.tickers = get_sp100_tickers()
        self.cash = INITIAL_CAPITAL
        self.portfolio = {}
        self.history = []
        self.trade_log = []

    def run(self):
        print(f"Stahuji data pro {len(self.tickers)} titulů...")

        raw_data = yf.download(
            self.tickers,
            start=DATA_START,
            end=DATA_END,
            interval="1d",
            progress=True,
            auto_adjust=True,
            group_by="column"
        )

        close_data = raw_data['Close']
        high_data = raw_data['High']
        low_data  = raw_data['Low']

        # index startu simulace (aby RSI měl warmup)
        sim_start_ts = pd.Timestamp(SIM_START)
        start_i = close_data.index.searchsorted(sim_start_ts)

        print("Simuluji strategii...")

        for i in range(max(1, start_i), len(close_data)):
            timestamp = close_data.index[i]
            if timestamp > pd.Timestamp(SIM_END):
                break

            # --- 1) SELL ---
            to_sell = []
            for symbol, pos in list(self.portfolio.items()):
                day_high = high_data.iloc[i].get(symbol, np.nan)
                day_low  = low_data.iloc[i].get(symbol, np.nan)
                if np.isnan(day_high) or np.isnan(day_low):
                    continue

                if day_high >= pos['price'] * (1 + SELL_GAIN):
                    self.execute_sell(symbol, pos['price'] * (1 + SELL_GAIN), timestamp, "Take Profit")
                    to_sell.append(symbol)
                elif USE_STOP_LOSS and day_low <= pos['price'] * (1 - STOP_LOSS_PCT):
                    self.execute_sell(symbol, pos['price'] * (1 - STOP_LOSS_PCT), timestamp, "Stop Loss")
                    to_sell.append(symbol)

            for s in to_sell:
                self.portfolio.pop(s, None)

            # --- 2) BUY ---
            if len(self.portfolio) < MAX_POSITIONS:
                potential_buys = []
                for symbol in self.tickers:
                    if symbol in self.portfolio or symbol not in close_data.columns:
                        continue

                    price_series = close_data[symbol].iloc[:i+1].dropna()
                    if len(price_series) < RSI_PERIOD + 1:
                        continue

                    prev_close = price_series.iloc[-2]
                    day_low = low_data.iloc[i].get(symbol, np.nan)
                    if np.isnan(prev_close) or np.isnan(day_low):
                        continue

                    if day_low <= prev_close * (1 - BUY_DROP):
                        rsi = calculate_rsi_wilder(price_series, RSI_PERIOD).iloc[-1]
                        if not np.isnan(rsi) and rsi < RSI_LIMIT:
                            buy_price = prev_close * (1 - BUY_DROP)
                            potential_buys.append((symbol, buy_price, float(rsi)))

                potential_buys.sort(key=lambda x: x[2])
                for symbol, price, rsi in potential_buys:
                    if len(self.portfolio) >= MAX_POSITIONS:
                        break
                    self.execute_buy(symbol, price, timestamp)

            # --- EQUITY ---
            current_port_value = 0.0
            for s in self.portfolio:
                px = close_data.iloc[i].get(s, np.nan)
                if not np.isnan(px):
                    current_port_value += self.portfolio[s]['qty'] * px

            self.history.append({'Date': timestamp, 'Equity': self.cash + current_port_value})

        return self

    def execute_buy(self, symbol, price, time):
        allocation = INITIAL_CAPITAL / MAX_POSITIONS
        qty = int((allocation - FEE) / price)
        if qty > 0:
            self.cash -= (qty * price + FEE)
            self.portfolio[symbol] = {'qty': qty, 'price': price}
            self.trade_log.append({'Date': time, 'Symbol': symbol, 'Action': 'BUY', 'Price': price, 'Qty': qty})

    def execute_sell(self, symbol, price, time, reason):
        pos = self.portfolio[symbol]
        self.cash += (pos['qty'] * price - FEE)
        self.trade_log.append({'Date': time, 'Symbol': symbol, 'Action': f'SELL ({reason})', 'Price': price, 'Qty': pos['qty'], 'Reason': reason})

    def show_results(self):
        if not self.history:
            print("Žádná data.")
            return
        df = pd.DataFrame(self.history).set_index('Date')
        final_roi = ((df['Equity'].iloc[-1] / INITIAL_CAPITAL) - 1) * 100
        print(f"\n--- VÝSLEDKY ---")
        print(f"Konečná equity: ${df['Equity'].iloc[-1]:,.2f}")
        print(f"Celkové ROI: {final_roi:.2f}%")
        print(f"Celkem obchodů: {len(self.trade_log)}")

        if self.trade_log:
            tl = pd.DataFrame(self.trade_log)
            print("\nObchody:")
            print(tl.tail(50).to_string(index=False))

        df['Equity'].plot(figsize=(10, 5), grid=True, title="Vývoj Equity (Backtest S&P 100)")
        plt.show()

if __name__ == "__main__":
    Backtester().run().show_results()
