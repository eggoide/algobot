import os
import sqlite3
import pandas as pd

DB_PATH = os.getenv("DB_PATH", "/data/algobot.db")
CSV_PATH = os.getenv("CSV_PATH", "/data/trade_history.csv")

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;

    CREATE TABLE IF NOT EXISTS trades (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      action TEXT NOT NULL,
      symbol TEXT NOT NULL,
      price REAL NOT NULL,
      qty INTEGER NOT NULL,
      pnl REAL NOT NULL DEFAULT 0,
      note TEXT NOT NULL DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
    CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);

    -- ochrana proti duplicitám při importu
    CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_dedupe
      ON trades(ts, action, symbol, price, qty, pnl, note);
    """)

def normalize(df: pd.DataFrame) -> pd.DataFrame:
    # očekává: Date,Action,Symbol,Price,Quantity,PnL,Note
    df = df.copy()

    # názvy
    df.columns = [c.strip() for c in df.columns]

    # ts
    df["Date"] = df["Date"].astype(str).str.strip()

    # action
    df["Action"] = df["Action"].astype(str).str.strip().str.upper()
    df.loc[~df["Action"].isin(["BUY", "SELL"]), "Action"] = "UNKNOWN"

    # symbol
    df["Symbol"] = df["Symbol"].astype(str).str.strip()

    # čísla
    df["Price"] = pd.to_numeric(df["Price"], errors="coerce").fillna(0.0)
    df["PnL"] = pd.to_numeric(df["PnL"], errors="coerce").fillna(0.0)

    # quantity může být float v CSV -> převedeme na int
    df["Quantity"] = pd.to_numeric(df["Quantity"], errors="coerce").fillna(0).astype(int)

    # note
    df["Note"] = df["Note"].fillna("").astype(str)

    # vyhoď nesmysly
    df = df[df["Symbol"] != ""]
    df = df[df["Quantity"] != 0]
    df = df[df["Price"] > 0]

    return df

def main() -> None:
    if not os.path.exists(CSV_PATH):
        raise SystemExit(f"CSV not found: {CSV_PATH}")

    df = pd.read_csv(CSV_PATH)
    df = normalize(df)

    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    inserted = 0
    skipped = 0

    for _, r in df.iterrows():
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO trades(ts, action, symbol, price, qty, pnl, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["Date"],
                    r["Action"],
                    r["Symbol"],
                    float(r["Price"]),
                    int(r["Quantity"]),
                    float(r["PnL"]),
                    r["Note"],
                ),
            )
            # sqlite: pokud IGNORE, rowcount=0
            if conn.total_changes > inserted:
                inserted += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1

    conn.commit()

    # reálnější počty: conn.total_changes je kumulativní; vytiskneme radši count z DB:
    count = conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"]
    print(f"Import done. DB trades count={count}")
    print(f"Inserted approx={inserted}, skipped approx={skipped}")

    conn.close()

if __name__ == "__main__":
    main()

