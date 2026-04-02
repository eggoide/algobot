import sqlite3
import datetime
from typing import List, Tuple, Dict, Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,                 -- ISO datetime
  action TEXT NOT NULL,             -- BUY/SELL
  symbol TEXT NOT NULL,
  price REAL NOT NULL,
  qty INTEGER NOT NULL,
  pnl REAL NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
"""

def db_connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn

def insert_trade(conn: sqlite3.Connection, action: str, symbol: str, price: float, qty: int, pnl: float = 0.0, note: str = "") -> None:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO trades(ts, action, symbol, price, qty, pnl, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, action, symbol, float(price), int(qty), float(pnl), note or "")
    )
    conn.commit()

def last_trades(conn: sqlite3.Connection, limit: int = 10) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT ts, action, symbol, qty, price, pnl, note FROM trades ORDER BY ts DESC LIMIT ?",
        (int(limit),)
    ).fetchall()
    return [dict(r) for r in rows]

def get_buy_time(conn: sqlite3.Connection, symbol: str) -> str:
    """Get the timestamp of the most recent BUY for a symbol (that hasn't been sold yet)."""
    row = conn.execute(
        "SELECT ts FROM trades WHERE symbol = ? AND action = 'BUY' ORDER BY ts DESC LIMIT 1",
        (symbol,)
    ).fetchone()
    return row["ts"] if row else ""


def cumulative_pnl_series(conn: sqlite3.Connection) -> Tuple[List[str], List[float]]:
    rows = conn.execute(
        """
        SELECT ts,
               SUM(pnl) OVER (ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum_pnl
        FROM trades
        ORDER BY ts
        """
    ).fetchall()

    dates: List[str] = []
    values: List[float] = []
    for r in rows:
        dates.append(r["ts"].replace("T", " ")[0:16])
        values.append(float(r["cum_pnl"] or 0.0))
    return dates, values

