"""
Historical data loader for backtesting.
Downloads and caches OHLCV data from Yahoo Finance.
"""

import os
import datetime
import sqlite3
from typing import Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf


DEFAULT_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".algobot_cache")


def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def _cache_path(cache_dir: str, symbol: str, interval: str, auto_adjust: bool = True) -> str:
    suffix = "" if auto_adjust else "_raw"
    return os.path.join(cache_dir, f"{symbol}_{interval}{suffix}.parquet")


def download_symbol(
    symbol: str,
    period: str = "2y",
    interval: str = "1h",
    cache_dir: str = DEFAULT_CACHE_DIR,
    force_refresh: bool = False,
    max_cache_age_hours: int = 24,
    auto_adjust: bool = True,
) -> Optional[pd.DataFrame]:
    """Download OHLCV data for a single symbol, with Parquet caching."""
    _ensure_dir(cache_dir)
    cp = _cache_path(cache_dir, symbol, interval, auto_adjust)

    # Check cache
    if not force_refresh and os.path.exists(cp):
        try:
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(cp))
            age_h = (datetime.datetime.now() - mtime).total_seconds() / 3600
            if age_h < max_cache_age_hours:
                df = pd.read_parquet(cp)
                if not df.empty:
                    return df
        except Exception:
            pass

    # Download
    try:
        df = yf.download(
            symbol,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=auto_adjust,
            threads=False,
        )
        if df.empty:
            return None

        # Flatten MultiIndex columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Save cache
        try:
            df.to_parquet(cp)
        except Exception:
            pass

        return df
    except Exception as e:
        print(f"[data_loader] Error downloading {symbol}: {e}")
        return None


def download_multiple(
    symbols: List[str],
    period: str = "2y",
    interval: str = "1h",
    cache_dir: str = DEFAULT_CACHE_DIR,
    force_refresh: bool = False,
    max_cache_age_hours: int = 24,
    auto_adjust: bool = True,
) -> Dict[str, pd.DataFrame]:
    """Download data for multiple symbols. Returns dict of symbol -> DataFrame."""
    result = {}
    total = len(symbols)
    for i, sym in enumerate(symbols):
        print(f"  [{i+1}/{total}] {sym}...", end=" ", flush=True)
        df = download_symbol(sym, period, interval, cache_dir, force_refresh, max_cache_age_hours, auto_adjust)
        if df is not None and not df.empty:
            result[sym] = df
            print(f"{len(df)} bars")
        else:
            print("SKIP (no data)")
    return result


def download_bulk(
    symbols: List[str],
    period: str = "2y",
    interval: str = "1h",
    cache_dir: str = DEFAULT_CACHE_DIR,
    force_refresh: bool = False,
    max_cache_age_hours: int = 24,
    auto_adjust: bool = True,
) -> Dict[str, pd.DataFrame]:
    """
    Bulk download using yfinance's multi-ticker support.
    Faster than individual downloads for many symbols.
    Falls back to individual download on error.
    """
    _ensure_dir(cache_dir)

    # Check if all cached
    if not force_refresh:
        all_cached = True
        cached = {}
        for sym in symbols:
            cp = _cache_path(cache_dir, sym, interval, auto_adjust)
            if os.path.exists(cp):
                try:
                    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(cp))
                    age_h = (datetime.datetime.now() - mtime).total_seconds() / 3600
                    if age_h < max_cache_age_hours:
                        df = pd.read_parquet(cp)
                        if not df.empty:
                            cached[sym] = df
                            continue
                except Exception:
                    pass
            all_cached = False

        if all_cached and cached:
            print(f"[data_loader] All {len(cached)} symbols loaded from cache")
            return cached

    # Bulk download
    print(f"[data_loader] Bulk downloading {len(symbols)} symbols ({period}, {interval})...")
    try:
        data = yf.download(
            symbols,
            period=period,
            interval=interval,
            progress=True,
            group_by='ticker',
            auto_adjust=auto_adjust,
            threads=True,
        )

        result = {}
        for sym in symbols:
            try:
                if isinstance(data.columns, pd.MultiIndex):
                    df = data[sym].copy()
                else:
                    df = data.copy()

                df = df.dropna(subset=['Close'])
                if df.empty:
                    continue

                # Cache
                cp = _cache_path(cache_dir, sym, interval, auto_adjust)
                try:
                    df.to_parquet(cp)
                except Exception:
                    pass

                result[sym] = df
            except Exception:
                continue

        print(f"[data_loader] Downloaded {len(result)}/{len(symbols)} symbols")
        return result

    except Exception as e:
        print(f"[data_loader] Bulk download failed: {e}. Falling back to individual downloads.")
        return download_multiple(symbols, period, interval, cache_dir, force_refresh, max_cache_age_hours, auto_adjust)


def get_sp100_tickers() -> List[str]:
    """Get S&P 100 ticker list from Wikipedia or fallback."""
    fallback = ['AAPL', 'MSFT', 'GOOG', 'AMZN', 'NVDA', 'META', 'JPM', 'WMT', 'PG', 'XOM',
                'JNJ', 'V', 'MA', 'HD', 'DIS', 'BAC', 'ADBE', 'CRM', 'NFLX', 'AMD',
                'INTC', 'CSCO', 'PEP', 'KO', 'ABT', 'MRK', 'TMO', 'COST', 'AVGO', 'QCOM',
                'TXN', 'UNH', 'LLY', 'CVX', 'MCD', 'WFC', 'PM', 'NEE', 'LOW', 'UPS',
                'MS', 'GS', 'BLK', 'AXP', 'CAT', 'BA', 'GE', 'RTX', 'HON', 'NOW']
    try:
        import requests
        from io import StringIO

        url = "https://en.wikipedia.org/wiki/S%26P_100"
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120 Safari/537.36"}
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code != 200:
            return fallback

        tables = pd.read_html(StringIO(resp.text))
        df = next((t for t in tables if 'Symbol' in t.columns), None)
        if df is None:
            return fallback

        tickers = [str(s).strip().replace('.', '-') for s in df['Symbol'].tolist()]
        tickers = [t for t in tickers if t]
        return tickers if len(tickers) >= 50 else fallback

    except Exception:
        return fallback


def slice_data_at_bar(data: Dict[str, pd.DataFrame], bar_idx: int) -> Dict[str, pd.DataFrame]:
    """
    Return data sliced up to bar_idx (inclusive) for each symbol.
    Prevents look-ahead bias in backtesting.
    """
    sliced = {}
    for sym, df in data.items():
        if bar_idx < len(df):
            sliced[sym] = df.iloc[:bar_idx + 1]
    return sliced
