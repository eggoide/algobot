# AlgoBot

Automatizovaný trading bot pro **Interactive Brokers (IB)** běžící v Dockeru.  
Projekt využívá **IB Gateway**, **Python (ib_insync)**, **SQLite** pro perzistentní historii obchodů a **statický HTML dashboard** servírovaný přes nginx.

---

## ✨ Vlastnosti

- Paper trading přes **IB Gateway**
- Automatické BUY / SELL strategie
- Perzistentní historie obchodů v **SQLite**
- HTML dashboard s PnL, portfoliem a historií
- Oddělené kontejnery:
  - IB Gateway
  - Trading bot
  - Dashboard (nginx)
- Odolné vůči restartům (state + dedupe)
- Připravené na dlouhodobý běh (server / VPS)

---

## 🧱 Architektura

```
IB Gateway (Docker)
        │
        │ IB API
        ▼
AlgoBot (Python, ib_insync)
        │
        │ SQLite + HTML report
        ▼
Dashboard (nginx)
```

---

## 📁 Struktura projektu

```
algobot/
├── app/
│   ├── bot.py
│   ├── db.py
│   ├── wait_for_port.py
│   ├── import_csv_to_sqlite.py
│   ├── config.yaml
│   └── requirements.txt
│
├── docker/
│   └── bot/
│       └── Dockerfile
│
├── volumes/          # runtime data (NEcommitovat)
│   ├── data/         # SQLite DB, CSV
│   ├── reports/      # index.html (dashboard)
│   └── ib_settings/  # IB Gateway settings
│
├── compose.yml
├── .env.example
├── .gitignore
├── .dockerignore
└── README.md
```

---

## 🔐 Konfigurace

Vytvoř `.env` ze vzoru:

```bash
cp .env.example .env
```

Soubor `.env` **nikdy necommituj** – obsahuje citlivé údaje.

---

## ▶️ Spuštění

```bash
docker compose build
docker compose up -d
```

Logy:

```bash
docker logs -f algobot
docker logs -f ib-gateway
```

---

## 📊 Dashboard

Dashboard je statický HTML report generovaný botem.

- URL: `http://localhost:8080`
- Report se generuje během obchodních hodin NYSE nebo při startu jako placeholder.

---

## 🖥️ VNC – IB Gateway

Přístup k IB Gateway (login / 2FA):

```
<server-ip>:5900
```

Doporučeno používat SSH tunnel:

```bash
ssh -L 5900:localhost:5900 user@server
```

---

## 🗄️ SQLite

- Umístění DB (v kontejneru): `/data/algobot.db`
- Slouží pro historii, reporting a PnL
- Source of truth pro otevřené pozice je **Interactive Brokers**, ne SQLite

---

## 📥 Import historie

```bash
docker compose exec bot python /app/import_csv_to_sqlite.py
```

CSV musí být v `volumes/data/trade_history.csv`.

---

## 🚧 Roadmap

- ukládání executions z IB
- partial fills handling
- více strategií
- live trading režim
- CI (docker build check)
- alerty na drawdown

---

## ⚠️ Upozornění

Projekt je určen pro **paper trading / experimenty**.  
Použití na live účtu je **na vlastní riziko**.

---

## 👤 Autor

Ondřej Musil  
GitHub: https://github.com/eggoide
