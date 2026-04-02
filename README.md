# AlgoBot

Automatizovaný trading bot pro **Interactive Brokers (IB)** běžící v Dockeru.
Projekt využívá **IB Gateway**, **Python (ib_insync)**, **SQLite** pro perzistentní historii obchodů a **statický HTML dashboard** servírovaný přes nginx.

---

## Vlastnosti

- Paper trading přes **IB Gateway**
- **Enhanced strategie** s RSI, MACD, Bollinger Bands, trailing stop, time stop
- Backtesting framework s optimalizací parametrů a walk-forward analýzou
- Perzistentní historie obchodů v **SQLite**
- HTML dashboard s PnL, portfoliem a historií
- Oddělené kontejnery:
  - IB Gateway
  - Trading bot
  - Dashboard (nginx)
- Odolné vůči restartům (state + dedupe)
- Telegram notifikace
- Připravené na dlouhodobý běh (server / VPS)

---

## Architektura

```
IB Gateway (Docker)
        |
        | IB API
        v
AlgoBot (Python, ib_insync)
   |         |
   | SQLite  | strategy.py + indicators.py
   v         v
Dashboard   Backtester
(nginx)     (run_backtest.py)
```

---

## Struktura projektu

```
algobot/
├── app/
│   ├── bot.py                  # Hlavní bot loop + dashboard generátor
│   ├── db.py                   # SQLite schema a helpery
│   ├── indicators.py           # Technické indikátory (RSI, MACD, BB, ATR, SMA, EMA)
│   ├── strategy.py             # Abstrakce strategií (DipBuy + EnhancedDipBuy)
│   ├── config.yaml             # Konfigurace strategie a parametrů
│   ├── run_backtest.py         # CLI pro backtesting
│   ├── requirements.txt        # Python závislosti
│   ├── wait_for_port.py        # Startup helper (čeká na IB Gateway)
│   ├── import_csv_to_sqlite.py # Import CSV do SQLite
│   ├── sp100_tickers_cache.txt # Cache S&P 100 tickerů
│   └── backtest/               # Backtesting framework
│       ├── __init__.py
│       ├── engine.py           # Backtest engine + ParameterOptimizer
│       ├── data_loader.py      # Stahování a cachování historických dat
│       ├── portfolio.py        # Simulace portfolia
│       ├── metrics.py          # Výpočet metrik (Sharpe, MaxDD, win rate...)
│       └── report.py           # HTML report generátor
│
├── docker/
│   └── bot/
│       └── Dockerfile
│
├── volumes/                    # Runtime data (NEcommitovat)
│   ├── data/                   # SQLite DB, logy, state
│   └── reports/                # HTML dashboard, status.json
│
├── compose.yml
├── .env.example
├── .gitignore
├── .dockerignore
└── README.md
```

---

## Konfigurace

### .env

Vytvoř `.env` ze vzoru:

```bash
cp .env.example .env
```

Soubor `.env` **nikdy necommituj** – obsahuje citlivé údaje (IB credentials, Telegram token).

### config.yaml

Hlavní konfigurace strategie v `app/config.yaml`:

```yaml
capital:
  manual_capital_limit: 10000    # Kapitál na obchodování
  max_positions: 5               # Max současných pozic
  fee_usd: 1.0                  # Poplatek za obchod

strategy:
  dip_mode: DAILY               # DAILY / HOURLY referenční cena
  buy_drop: 0.02                # Nákup při poklesu 2%
  sell_gain: 0.03               # Prodej při zisku 3%

  use_stop_loss: true           # Stop-loss aktivní
  stop_loss: 0.07               # Stop-loss na -7%

  rsi_limit: 30                 # Nákup jen při RSI < 30
  rsi_period: 14

  # Enhanced strategie
  use_trailing_stop: true       # Trailing stop (prodej při poklesu od maxima)
  trailing_stop_pct: 0.02       # 2% od high water mark

  use_time_stop: true           # Časový stop
  time_stop_bars: 120           # Zavřít po ~5 dnech (120 hodinových barů)

  use_macd: true                # MACD konfirmace signálu
  use_bollinger: true           # Bollinger Bands konfirmace
  use_volume_filter: false      # Volume filtr (volitelný)
  use_sma_filter: false         # SMA 200 filtr (volitelný)
```

---

## Spuštění

### Live bot (Docker)

```bash
# Build a spuštění všech služeb
docker compose build
docker compose up -d

# Nebo jednotlivě
docker compose build bot
docker compose up -d ib-gateway bot dashboard
```

### Kontrola stavu

```bash
# Stav kontejnerů
docker compose ps

# Logy bota
docker compose logs -f bot

# Logy IB Gateway
docker compose logs -f ib-gateway

# Restart bota (po změně config.yaml nebo kódu)
docker compose restart bot
```

Po změně `requirements.txt` nebo `Dockerfile` je nutný rebuild:

```bash
docker compose build bot && docker compose up -d bot
```

---

## Dashboard

- URL: `http://<server-ip>:8080`
- Auto-refresh každých 60s (trh otevřený) / 600s (zavřený)
- Live status a logy přes AJAX polling

---

## Backtesting

Backtester běží lokálně (mimo Docker) a nevyžaduje IB Gateway.

### Instalace závislostí (lokálně)

```bash
cd app
pip install -r requirements.txt
```

### Spuštění backtestu

```bash
cd app

# Základní backtest s původní DipBuy strategií
python3 run_backtest.py

# Enhanced strategie
python3 run_backtest.py --strategy enhanced

# Vlastní symboly a období
python3 run_backtest.py --strategy enhanced --symbols AAPL,MSFT,NVDA,AMD,META --period 1y

# Denní interval místo hodinového
python3 run_backtest.py --strategy enhanced --interval 1d --period 2y

# Vlastní kapitál a pozice
python3 run_backtest.py --capital 50000 --max-positions 10

# S benchmarkem
python3 run_backtest.py --strategy enhanced --benchmark SPY
```

### Optimalizace parametrů (grid search)

```bash
# Grid search - najde nejlepší kombinaci parametrů
python3 run_backtest.py --optimize --strategy enhanced

# Optimalizace podle jiné metriky
python3 run_backtest.py --optimize --optimize-metric total_return_pct
python3 run_backtest.py --optimize --optimize-metric profit_factor
python3 run_backtest.py --optimize --optimize-metric win_rate
```

### Walk-forward analýza

```bash
# Walk-forward: optimalizace na train okně, test na dalším
python3 run_backtest.py --walk-forward --strategy enhanced

# Vlastní velikost oken
python3 run_backtest.py --walk-forward --train-bars 1500 --test-bars 500
```

### Parametry run_backtest.py

| Parametr | Default | Popis |
|----------|---------|-------|
| `--strategy` | `dip_buy` | Strategie (`dip_buy` nebo `enhanced`) |
| `--period` | `2y` | Období dat (1mo, 3mo, 6mo, 1y, 2y, 5y) |
| `--interval` | `1h` | Interval (1m, 5m, 15m, 1h, 1d) |
| `--symbols` | S&P 100 | Symboly oddělené čárkou |
| `--capital` | z configu | Počáteční kapitál |
| `--max-positions` | z configu | Max pozic |
| `--benchmark` | `AAPL` | Benchmark pro porovnání |
| `--start-date` | - | Začátek (YYYY-MM-DD) |
| `--end-date` | - | Konec (YYYY-MM-DD) |
| `--refresh` | false | Vynutit stažení nových dat |
| `--cache-hours` | 72 | Max stáří cache v hodinách |
| `--optimize` | false | Spustit grid search |
| `--walk-forward` | false | Spustit walk-forward analýzu |
| `--optimize-metric` | `sharpe_ratio` | Metrika pro optimalizaci |
| `--train-bars` | 1000 | Walk-forward: velikost train okna |
| `--test-bars` | 250 | Walk-forward: velikost test okna |

### Výstupy backtestu

- **Terminál**: Tabulka s metrikami (return, Sharpe, win rate, max drawdown...)
- **HTML report**: `~/.algobot_cache/reports/backtest_<strategie>.html`
- **Cache dat**: `~/.algobot_cache/` (Parquet soubory)

---

## Strategie

### DipBuy (původní)

Jednoduchá mean-reversion strategie:
- **Nákup**: RSI < 30 AND cena klesla >= 2% od referenční ceny
- **Prodej**: zisk >= 3% (Take Profit) nebo ztráta >= 7% (Stop Loss)

### EnhancedDipBuy (aktuální)

Vylepšená strategie se scoring systémem a dalšími indikátory:

**Nákupní signál** (všechny podmínky musí platit):
- RSI < 30 (oversold)
- Cena klesla >= 2% od referenční ceny
- MACD histogram se otáčí nahoru (bullish divergence)
- Cena blízko spodního Bollinger Bandu (oversold konfirmace)
- Vážené skóre ze všech indikátorů určí sílu signálu

**Prodejní signály** (stačí jeden):
- **Take Profit**: zisk >= 3%
- **Stop Loss**: ztráta >= 7%
- **Trailing Stop**: cena klesla 2% od maxima (jen když v zisku)
- **Time Stop**: pozice držena > 120 hodin (~5 dní)

---

## Moduly

### indicators.py

Sdílený modul technických indikátorů:
- `rsi_wilder()` — RSI (Wilder's smoothing)
- `macd()` — MACD line, signal line, histogram
- `bollinger_bands()` — upper, middle, lower band, %B
- `atr()` — Average True Range
- `sma()`, `ema()` — Moving Averages
- `volume_sma()` — Volume Moving Average

### strategy.py

Abstraktní `Strategy` base class s rozhraním:
- `generate_signals(data, existing_positions)` — generuje nákupní signály
- `should_exit(symbol, entry_price, current_price, holding_bars)` — kontroluje výstupní podmínky

Dvě implementace: `DipBuyStrategy`, `EnhancedDipBuyStrategy`.

---

## VNC – IB Gateway

Přístup k IB Gateway GUI (login / 2FA / disclaimer):

```
<server-ip>:5900
```

SSH tunnel:

```bash
ssh -L 5900:localhost:5900 user@server
```

Po restartu IB Gateway může být nutné přijmout "Paper Trading Disclaimer" přes VNC.

---

## SQLite

- Umístění DB (v kontejneru): `/data/algobot.db`
- Slouží pro historii, reporting a PnL
- Source of truth pro otevřené pozice je **Interactive Brokers**, ne SQLite

### Import historie

```bash
docker compose exec bot python /app/import_csv_to_sqlite.py
```

CSV musí být v `volumes/data/trade_history.csv`.

---

## Upozornění

Projekt je určen pro **paper trading / experimenty**.
Použití na live účtu je **na vlastní riziko**.

---

## Autor

Ondřej Musil
GitHub: https://github.com/eggoide
