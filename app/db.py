import sqlite3
import datetime
from typing import List, Tuple, Dict, Any, Optional

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,                 -- ISO datetime
  action TEXT NOT NULL,             -- BUY/SELL
  symbol TEXT NOT NULL,
  price REAL NOT NULL,              -- actual fill price
  qty INTEGER NOT NULL,             -- filled quantity
  pnl REAL NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  ib_order_id INTEGER,              -- IB orderId (NULL for legacy rows / backtest)
  requested_price REAL,             -- price we asked for (slippage = price - requested_price)
  commission REAL,                  -- actual IB commission
  status TEXT                       -- Filled / PartiallyFilled / etc.
);

CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);

CREATE VIEW IF NOT EXISTS daily_pnl AS
SELECT date(ts) AS day,
       ROUND(SUM(pnl), 2) AS pnl_usd,
       COUNT(*) AS trades_count,
       SUM(CASE WHEN action = 'BUY' THEN 1 ELSE 0 END) AS buys,
       SUM(CASE WHEN action = 'SELL' THEN 1 ELSE 0 END) AS sells
FROM trades
GROUP BY date(ts);
"""

def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER for older DBs missing the V2 columns."""
    have = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    for col, ddl in (
        ("ib_order_id",     "ALTER TABLE trades ADD COLUMN ib_order_id INTEGER"),
        ("requested_price", "ALTER TABLE trades ADD COLUMN requested_price REAL"),
        ("commission",      "ALTER TABLE trades ADD COLUMN commission REAL"),
        ("status",          "ALTER TABLE trades ADD COLUMN status TEXT"),
    ):
        if col not in have:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass

def db_connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    conn.commit()
    return conn

def insert_trade(
    conn: sqlite3.Connection,
    action: str,
    symbol: str,
    price: float,
    qty: int,
    pnl: float = 0.0,
    note: str = "",
    ib_order_id: Optional[int] = None,
    requested_price: Optional[float] = None,
    commission: Optional[float] = None,
    status: Optional[str] = None,
) -> None:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO trades(ts, action, symbol, price, qty, pnl, note, "
        "ib_order_id, requested_price, commission, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts, action, symbol, float(price), int(qty), float(pnl), note or "",
            int(ib_order_id) if ib_order_id is not None else None,
            float(requested_price) if requested_price is not None else None,
            float(commission) if commission is not None else None,
            status,
        ),
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

