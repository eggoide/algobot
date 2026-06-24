# AlgoBot

Automatizovaný trading bot pro **Interactive Brokers (IB)** běžící v Dockeru.
Projekt využívá **IB Gateway**, **Python (ib_insync)**, **SQLite** pro perzistentní historii obchodů a **statický HTML dashboard** servírovaný přes nginx.

---

## Vlastnosti

- Paper trading přes **IB Gateway**
- **Enhanced strategie** s RSI, MACD, Bollinger Bands, trailing stop, time stop
- Backtesting framework s optimalizací parametrů a walk-forward analýzou
- Perzistentní historie obchodů v **SQLite** (včetně IB order ID, requested price, skutečné commission, fill status)
- HTML dashboard s PnL, portfoliem a historií + Pause Trading toggle
- Oddělené kontejnery:
  - IB Gateway
  - Trading bot
  - Dashboard (nginx)
  - Flask backtest service (`algobot-web`)
  - Heartbeat watchdog (Telegram alert pokud bot přestane updatovat `status.json`)
- Odolné vůči restartům — perzistovaný state (high-water marks, BUY/SELL cooldowns) + reconciliation IB pozic vs DB při startu
- **PAPER/LIVE account guard** — abort při mismatchi `TRADING_MODE` env vs IB account prefix (DU* paper / U* live)
- **NYSE kalendář svátků** přes `pandas_market_calendars` (Memorial Day, Thanksgiving, ranní zavření atd.)
- **Wait-for-fill** ověření BUY i SELL obchodů přes IB (do DB se zapisuje až po reálném fillu; partial fill se ukládá s reálnou qty)
- Telegram notifikace s retry (`HTTPAdapter` max_retries=3)
- Unit testy (`tests/`, pytest) pro `indicators.py` a `strategy.py`
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
│   ├── bot.py                  # Hlavní bot loop, IB orchestrace, dashboard generátor
│   ├── scheduler.py            # Pure NYSE schedule + cycle id helpers (extrahováno z bot.py)
│   ├── db.py                   # SQLite schema + idempotentní migrace + daily_pnl view
│   ├── indicators.py           # Technické indikátory (RSI, MACD, BB, ATR, SMA, EMA)
│   ├── strategy.py             # Abstrakce strategií (DipBuy + EnhancedDipBuy)
│   ├── config.yaml             # Konfigurace strategie + runtime (universe_mode, fixed_universe...)
│   ├── run_backtest.py         # CLI pro backtesting
│   ├── requirements.txt        # Python závislosti (bot, pinned)
│   ├── wait_for_port.py        # Startup helper (čeká na IB Gateway)
│   ├── import_csv_to_sqlite.py # Import CSV do SQLite
│   ├── backtest/               # Backtesting framework
│   │   ├── engine.py           # Backtest engine + ParameterOptimizer (slippage, IB fees, next-open fill)
│   │   ├── data_loader.py      # Stahování a cachování historických dat
│   │   ├── portfolio.py        # Simulace portfolia (slippage + fee model)
│   │   ├── metrics.py          # Výpočet metrik (Sharpe, MaxDD, win rate...)
│   │   └── report.py           # HTML report generátor (CLI)
│   └── web/                    # Flask /backtest service
│       ├── app.py              # Routes + blueprint
│       ├── jobs.py             # In-memory async job store
│       ├── runner.py           # run_single + run_replay (live vs backtest)
│       ├── requirements.txt    # Flask + gunicorn závislosti
│       ├── templates/          # _base, index, result, replay (Jinja)
│       └── static/             # style.css
│
├── tests/                      # pytest unit testy (V3)
│   ├── conftest.py             # pridává app/ do sys.path
│   ├── test_indicators.py      # RSI, MACD, BB, ATR sanity checks
│   ├── test_strategy.py        # should_exit větve (TP, SL, trailing, time stop)
│   └── README.md
│
├── docker/
│   ├── bot/Dockerfile
│   ├── web/Dockerfile          # Flask container (gunicorn)
│   ├── dashboard/nginx.conf    # Reverse proxy /backtest/* + /v2/api/* → web:8081; Basic Auth
│   ├── dashboard/.htpasswd    # Hesla (NECOMMITOVAT — v .gitignore)
│   └── heartbeat/heartbeat.sh  # Watchdog: hlídá mtime status.json + Telegram alert
│
├── volumes/                    # Runtime data (NEcommitovat)
│   ├── data/                   # SQLite DB, logy, state, SP100 cache
│   └── reports/                # HTML dashboard, status.json, snapshot JSONy
│
├── compose.yml                 # 5 služeb: ib-gateway, bot, web, dashboard, heartbeat
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

Hlavní konfigurace strategie v `app/config.yaml`. Klíčové sekce: `capital`, `strategy`, `runtime` (timezone, universe), `backtest`, `report`. Příklad strategy bloku:

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
  time_stop_bars: 240           # POZOR: v live běhu se počítá ve wall-clock hodinách (viz Známé limity)

  use_macd: true                # MACD konfirmace signálu
  use_bollinger: true           # Bollinger Bands konfirmace
  use_volume_filter: false      # Volume filtr (volitelný)
  use_sma_filter: false         # SMA 200 filtr (volitelný)
```

`runtime.universe_mode` (V7) volí mezi `sp100_live` (Wikipedia scrape, survivorship-biased) a `fixed` (pevný blue-chip seznam ze `runtime.fixed_universe`). Default `sp100_live`.

---

## Testy

```bash
pip install pytest
python3 -m pytest tests/ -q
```

`tests/conftest.py` přidává `app/` do `sys.path`, takže testy běží bez instalace. 17 testů pro `indicators` a `strategy`.

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
docker compose build bot && docker compose up -d
```

> **Pozn.:** Vždy `docker compose up -d` (bez konkrétního service), jinak compose recreatuje
> síť a sourozenecké kontejnery (`dashboard`) v ní zůstanou odpojené.

---

## Dashboard

- URL: `http://<server-ip>:8080`
- Auto-refresh každých 60s (trh otevřený) / 600s (zavřený)
- Live status a logy přes AJAX polling
- **Přístup chráněn HTTP Basic Auth** (vyžadováno při přístupu z internetu)

### Přihlášení — nastavení hesla

Dashboard je chráněn nginx Basic Auth. Heslo není součástí repozitáře — soubor `.htpasswd` musíš vytvořit ručně na každém stroji, kde projekt běží.

**Vytvoření `.htpasswd` souboru:**

```bash
# Varianta A — pomocí nástroje htpasswd (balík apache2-utils / httpd-tools)
sudo apt install apache2-utils   # Debian/Ubuntu
htpasswd -c docker/dashboard/.htpasswd tvoje_jmeno

# Varianta B — bez instalace, čistě přes openssl
echo "tvoje_jmeno:$(openssl passwd -apr1 'tvojeHeslo')" > docker/dashboard/.htpasswd
```

Soubor `docker/dashboard/.htpasswd` je v `.gitignore` — nikdy ho necommituj.

**Přidání / změna hesla** (pro dalšího uživatele nebo reset bez přepsání souboru):

```bash
# Přidat dalšího uživatele (bez -c, který by přepsal soubor)
htpasswd docker/dashboard/.htpasswd druhy_uzivatel
```

**Aplikování změn** — po vytvoření nebo úpravě `.htpasswd` musí být dashboard container recreatován (restart nestačí, pokud ještě soubor nebyl namountován):

```bash
docker compose up -d --force-recreate dashboard
```

> **Bezpečnostní poznámka:** Basic Auth bez HTTPS posílá heslo v Base64 (nešifrovaně). Na IP adrese bez HTTPS doporučuji doplnit firewall pravidlo povolující přístup jen z tvé IP. Pro nasazení s doménou přidej Let's Encrypt certifikát do nginx — `auth_basic` direktivy zůstanou beze změny.

### `/backtest` — webový backtester

- URL: `http://<server-ip>:8080/backtest/`
- Realistický execution model: **slippage**, **IB fee** (max $1, $0.005/share), **fill na Open dalšího baru** (žádný look-ahead bias)
- Formulář: strategie, symboly, období, parametry strategie (přepíší config.yaml)
- Async joby s polling statusem — můžeš zavřít browser, job běží v containeru
- **Live vs Backtest Replay**: jedno tlačítko vezme tvoje skutečné trades z `algobot.db`, spustí backtest na stejných symbolech a období, vypíše divergenci po symbolu
- JSON API: `POST /backtest/run`, `POST /backtest/replay`, `GET /backtest/job/<id>`, `GET /backtest/result/<id>`
- Servisováno přes Flask v kontejneru `algobot-web` (port 8081 interně, nginx proxy `/backtest/*`)

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
- **Time Stop**: pozice držena déle než `time_stop_bars` (live: wall-clock hodiny, backtest: počet barů — viz Známé limity)

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

## Backtest accuracy model

Backtest engine (`app/backtest/engine.py` + `portfolio.py`) byl 2026-05-25 přepracován pro realistický run, aby čísla odpovídala live obchodování v IB paper:

| Vlastnost | Default | Význam |
|-----------|---------|--------|
| `backtest.slippage_pct` | `0.0005` (5 bp) | BUY platí o slippage víc, SELL dostane o slippage míň |
| `backtest.fee_model` | `ib` | `max($1, $0.005 × qty)` — skutečný IB ceník. `flat` = $1/trade pro zpětnou kompatibilitu |
| `backtest.use_next_open` | `true` | Signál na Close baru N → fill na Open baru N+1 (žádný look-ahead bias) |
| `backtest.auto_adjust` | `true` | yfinance dividend-adjusted close. `false` = raw close (lepší pro RSI/MACD signály na dividendových akciích) |

Time Stop má teď stejnou sémantiku v live i backtest: počet NYSE hodinových barů (přes `pandas_market_calendars`).

Validace: replay z live `algobot.db` (Dec 2025 → May 2026, 26 symbolů) vrátil divergenci jen 12 % (live `-$589` vs backtest `-$661`). Detail v `/backtest/replay/<id>`.

---

## Známé limity a roadmapa

### Zbývající body před přechodem na live (2026-06-05)

| # | Téma | Popis | Stav |
|---|------|-------|------|
| **B2** | **Daily kill-switch** | Gate v `main_loop` před `scan_and_buy`: `daily_realized_pnl_pct < -X %` → no BUY do půlnoci NY. Volitelně VIX gate. Brání katastrofickému dni při flash crashi. | TODO (doporučený další krok, vyžaduje backtest) |
| **B3** | **LimitOrder + posun SELL na 9:45** | Aktuálně všechno MarketOrder; první SELL v 9:35 NY chytá nejhorší execution. Buď LimitOrder ±0.3 % mid + fallback, nebo prostě posunout první SELL na 9:45. | TODO |
| **B4** | **Live market data** | `reqMarketDataType(3)` = DELAYED 15+ min. Stop loss reaguje na cenu starou ¼ hod. Pro live: předplatit IB market data sub (~10 USD/mě), přepnout na LIVE. Feature flag `MARKET_DATA_TYPE` v `.env`. | TODO (vyžaduje IB subscription) |
| **V1** | **Risk-based position sizing přes ATR** | Aktuálně stejná USD pozice pro PLTR (ATR 5 %) i WMT (ATR 1 %) → 5× rozdílné riziko. Fix: `qty = (capital × 0.01) / (atr_pct × price)`. Mění distribuci výnosů, **vyžaduje nový backtest**. | TODO |
| **V5** | **Plný refactor `bot.py`** | `bot.py` má ~1650 řádků. `scheduler.py` už vyextrahován; zbývá rozdělit na `ib_client.py`, `position_manager.py`, `buy_scanner.py`, `dashboard_writer.py`, `state.py`. Cíl: `bot.py` < 300 řádků. Hygiena, neblokuje live. | částečně |
| **bug** | **HWM seeding v `should_exit`** | Unit testy odhalily, že `_high_water_marks` se v `EnhancedDipBuyStrategy.should_exit` nikdy implicitně neseeduje (default == `current_price` → `>` vždy False). Trailing stop funguje jen pokud HWM nasype externí caller nebo state restore. Fix: `>=` v should_exit, nebo explicit seed v `scan_and_buy` po BUY fillu. | TODO |

### Hotovo v sezení 2026-06-05

| # | Téma | Detail |
|---|------|--------|
| **B1** | BUY fill verification | `placeOrder` → wait 8s na terminal status → do DB jen reálná `avgFillPrice` + `filled_qty`; partial fill se ukládá; cooldown 60 min v `_recent_buy_attempts`; WARN telegram při nefillu |
| **B5** | PAPER/LIVE guard | `verify_account_mode(ib)` po každém connect → abort při DU* (paper) vs U* (live) mismatchi vůči `TRADING_MODE` env |
| **B6** | State perzistence + reconciliation | `save_state` automaticky ukládá `high_water_marks` + `recent_sell_attempts` + `recent_buy_attempts`; `restore_runtime_state` re-hydratuje při startu (expirované cooldowny filtruje); `reconcile_positions` po startup dumpu loguje IB-vs-DB mismatch (+ Telegram WARN) |
| **B7** | Cash check `<=` → `<` | 1 znak, drobnost |
| **B8** | `accountSummary` staleness | `ib.sleep(0.2)` před `accountSummary()`; ib_insync auto-subscribuje při `connect()`, takže fresh-fetch trik není potřeba (původní `reqAccountUpdates(True)` deadlockoval event loop) |
| **V2** | DB schema fill metadata | `trades` má 4 nové sloupce (`ib_order_id`, `requested_price`, `commission`, `status`); idempotentní migrace pro staré DB |
| **V3** | Unit testy | `tests/` adresář, pytest, 17 testů (RSI/MACD/BB/ATR + should_exit větve) |
| **V4** | Pinned requirements | `yfinance==1.2.0` hard pin (memory: 0.2.50 broken); ostatní soft `>=X.Y,<X+1.0` |
| **V6** | Smazáno `app/bot.py.bak` | |
| **V7** | Survivorship bias mitigation | `runtime.universe_mode: sp100_live\|fixed` v `config.yaml`; `fixed_universe` seznam 30 long-term blue-chips. Default zůstává `sp100_live`. |
| **V8** | Heartbeat watchdog | `docker/heartbeat/heartbeat.sh` + service v compose.yml; hlídá mtime `status.json > 600s`, Telegram alert rate-limited na 1800s |
| **K1** | Telegram retry | `requests.Session` + `HTTPAdapter(max_retries=3)` pro Telegram API |
| **K2** | Version → git SHA | `status.json.version` čte git SHA (fallback `unknown`); `GIT_SHA` env var lze předat v compose pro container |
| **K3** | SP100 cache | `sp100_tickers_cache.txt` → `/data/sp100_tickers_cache.txt` (perzistentní volume) |
| **K4** | `daily_pnl` view | `CREATE VIEW daily_pnl AS SELECT date(ts), SUM(pnl), COUNT(*), buys, sells FROM trades GROUP BY date(ts)` |

### Trvalé limity (nezávislé na roadmapě)

| # | Téma | Popis |
|---|------|-------|
| 1 | **Survivorship bias v backtestu** | I s `universe_mode: fixed` (V7) je fixní seznam jen pragmatický kompromis. Pro skutečně bias-free backtest je potřeba point-in-time historický index list (CRSP nebo placené API). |
| 2 | **Joby v paměti** | `algobot-web` ukládá historie joů v RAM. Restart kontejneru je smaže. Pro perzistenci: Redis/sqlite. |
| 3 | **hourly data limit yfinance** | yfinance dává cca 730 dní hourly historie. Pro delší backtesty použij `--interval 1d`. |

---

## Migrace na cloud server (live trading)

Tento návod pokrývá kompletní přesun projektu na čistý cloud VM s dashboardem na portu 80 a připojením na reálný IB účet.

### 1. Požadavky na VM

- OS: Ubuntu 22.04 LTS nebo Debian 12 (doporučeno)
- RAM: min. **2 GB** (IB Gateway potřebuje ~1 GB sám)
- CPU: 1–2 vCPU stačí
- Disk: 10 GB
- Otevřené porty (firewall):

| Port | Přístup | Účel |
|------|---------|------|
| 22 | jen tvoje IP | SSH |
| 80 | jen tvoje IP | Dashboard |
| 5900 | **ZAVŘÍT** | VNC — přistupovat jen přes SSH tunel |
| 4001, 4002 | **ZAVŘÍT** | IB Gateway — jen interní Docker síť |

> **Důležité:** IB Gateway ani VNC nesmí být přístupné z internetu. Porty 4001/4002 nejsou v compose.yml vůbec nutné exponovat — jsou jen pro případ přímého přístupu z hostitele. VNC používej výhradně přes SSH tunel (viz níže).

---

### 2. Instalace Dockeru na VM

```bash
# Přihlášení na VM
ssh user@<server-ip>

# Instalace Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker   # nebo odhlásit/přihlásit

# Ověření
docker compose version
```

---

### 3. Přenos projektu na server

**Varianta A — git (doporučeno):**

```bash
git clone https://github.com/eggoide/algobot.git
cd algobot
```

**Varianta B — rsync z lokálního stroje:**

```bash
# Spustit LOKÁLNĚ
rsync -avz --exclude='.env' --exclude='volumes/data/' --exclude='__pycache__' \
  ~/algobot/ user@<server-ip>:~/algobot/
```

---

### 4. Konfigurace pro live trading

#### 4a. Upravit `compose.yml`

Dvě změny oproti paper výchozímu stavu:

```yaml
# Bot service — změnit IB_PORT z 4002 na 4001
bot:
  environment:
    IB_PORT: "4001"    # ← bylo "4002" (paper), live port je 4001

# Dashboard service — změnit port z 8080 na 80
dashboard:
  ports:
    - "80:80"          # ← bylo "8080:80"
```

#### 4b. Vytvořit `.env`

```bash
cp .env.example .env
nano .env
```

Vyplnit:

```env
TWS_USERID=U1234567        # tvoje live IB číslo účtu (U..., ne DU...)
TWS_PASSWORD=tvojeHeslo    # IB heslo
VNC_SERVER_PASSWORD=neco   # libovolné heslo pro VNC (interní)
TRADING_MODE=live          # ← klíčová změna; paper → live

TG_TOKEN=                  # Telegram bot token (volitelné)
TG_CHAT_ID=                # Telegram chat ID (volitelné)
```

> **Bezpečnostní guard:** Bot obsahuje kontrolu `verify_account_mode()` — při každém připojení ověří, že prefix IB účtu odpovídá `TRADING_MODE`. Pokud nastavíš `TRADING_MODE=live` a připojíš se na paper účet (DU...), bot se okamžitě ukončí s chybou. Chrání před nechtěným live obchodem na paper účtu i naopak.

#### 4c. Vytvořit `.htpasswd`

```bash
# Bez potřeby instalace dalšího nástroje
echo "admin:$(openssl passwd -apr1 'tvojeHeslo')" > docker/dashboard/.htpasswd
```

#### 4d. Připravit volume adresáře

```bash
mkdir -p volumes/data volumes/reports
```

#### 4e. Zkopírovat dashboard `index.html` ze starého serveru

Soubor `volumes/reports/index.html` je gitignorovaný — při `git clone` se nepřenese. Je nutné ho zkopírovat ručně ze starého stroje:

```bash
# Spustit LOKÁLNĚ (ze starého serveru / Raspberry Pi)
scp ~/algobot/volumes/reports/index.html user@<novy-server-ip>:~/algobot/volumes/reports/
```

Bez tohoto souboru nginx vrací 403 Forbidden i po správném přihlášení.

---

### 5. Přidat swap (pokud má VM méně než 2 GB RAM)

IB Gateway (Java) potřebuje ~500 MB RAM sám. Na VMs s 1 GB RAM bez swapu dochází k vyhladovění paměti (load average 50+, 90 % iowait). Swap přidej hned po spuštění:

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# Přežití po restartu
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Ověření
free -h
```

---

### 6. Spuštění

```bash
# Build
docker compose build

# Start všech služeb
docker compose up -d

# Ověření že vše běží
docker compose ps
```

Očekávaný výstup `docker compose ps` (všechny služby `running`):

```
NAME                 STATUS
ib-gateway           running
algobot              running
algobot-web          running
algobot-dashboard    running
algobot-heartbeat    running
```

---

### 7. První přihlášení do IB Gateway přes VNC

IB Gateway vyžaduje manuální přihlášení při prvním spuštění (zadání 2FA kódu z IB Key / SMS). Přístup přes SSH tunel:

```bash
# Spustit LOKÁLNĚ — tunel port 5900 na tvůj notebook
ssh -L 5900:localhost:5900 user@<server-ip> -N
```

Pak otevřít libovolný VNC klient na `localhost:5900` (heslo = `VNC_SERVER_PASSWORD` z `.env`).

V IB Gateway GUI:
1. Přihlásit se s live IB credentials
2. Potvrdit 2FA (IB Key / SMS)
3. Přijmout případný disclaimer pro paper/live trading
4. Okno nechat otevřené — IB Gateway běží na pozadí

Po úspěšném přihlášení se bot automaticky připojí (sleduj logy: `docker compose logs -f bot`).

### 7a. Nastavení Trusted IPs v IB Gateway (nutné pro API spojení)

Toto je **nejčastější příčina selhání API spojení** po migraci. IB Gateway ve výchozím stavu odmítá API spojení z jiných IP než localhost — bot se pak připojí přes TCP, ale handshake tiše vyprší (`TimeoutError`).

Zjisti Docker síť bota:

```bash
docker network inspect algobot_default | grep Subnet
# Např. 172.19.0.0/16
```

V IB Gateway GUI (`Configure → Settings → API → Settings`):
- ☑ **Enable ActiveX and Socket Clients** — musí být zaškrtnuto
- ☐ **Allow connections from localhost only** — musí být ODŠKRTNUTO
- **Trusted IPs** — přidej celý Docker subnet, např. `172.19.0.0/16`

> **Pozn.:** Konkrétní subnet závisí na stroji. Při každé migraci na nový server zkontroluj aktuální subnet přes `docker network inspect`.

---

### 8. Firewall (UFW)

```bash
sudo ufw allow 22/tcp      # SSH — VŽDY jako první!
sudo ufw allow from <tvoje-ip> to any port 80   # Dashboard jen pro tebe
sudo ufw enable

# Ověření
sudo ufw status
```

Pokud tvoje IP není statická, alternativa: VPN nebo `sudo ufw allow 80/tcp` (pak spoléháš jen na Basic Auth heslo).

---

### 9. Ověření funkčnosti

```bash
# Logy bota — hledej "Připojen k IB", "account: U...", "TRADING_MODE=live OK"
docker compose logs -f bot

# Dashboard
curl -u admin:tvojeHeslo http://localhost/
```

Dashboard v prohlížeči: `http://<server-ip>/`

---

### 10. Automatický start po restartu VM

Docker Compose služby mají `restart: always` — po restartu VM se spustí automaticky, **ale IB Gateway opět vyžaduje manuální 2FA přihlášení přes VNC**. IB Gateway v kontejneru si po restartu nepamatuje přihlašovací session — je nutné se znovu přihlásit.

```bash
# Restart VM → po spuštění ověřit stav
docker compose ps

# IB Gateway ještě není přihlášen → opakovat krok 6 (VNC)
```

---

### Shrnutí změn oproti paper/lokálnímu nasazení

| Co | Paper / lokálně | Cloud / live |
|----|-----------------|--------------|
| `TRADING_MODE` v `.env` | `paper` | `live` |
| `IB_PORT` v `compose.yml` | `4002` | `4001` |
| Dashboard port | `8080:80` | `80:80` |
| IB účet prefix | `DU...` (paper) | `U...` (live) |
| VNC přístup | přímý `localhost:5900` | SSH tunel |

---

## Upozornění

Projekt je určen primárně pro **paper trading / experimenty**.
Použití na live účtu je **na vlastní riziko**.

---

## Autor

Ondřej Musil
GitHub: https://github.com/eggoide
