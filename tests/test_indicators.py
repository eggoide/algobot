import math

import numpy as np
import pandas as pd
import pytest

from indicators import rsi_wilder, sma, ema, macd, bollinger_bands, atr


def test_rsi_wilder_constant_series_is_undefined():
    """No gains, no losses → RSI is undefined (NaN). Confirms we never get a fake 100."""
    s = pd.Series([100.0] * 30)
    out = rsi_wilder(s, period=14)
    assert math.isnan(out.iloc[-1])


def test_rsi_wilder_strong_uptrend_above_70():
    # Mostly up with realistic noise (some negative deltas so avg_loss > 0).
    rng = np.random.RandomState(1)
    deltas = rng.normal(loc=0.8, scale=1.0, size=200)  # mean positive, but some losses
    s = pd.Series(100 + np.cumsum(deltas))
    out = rsi_wilder(s, period=14)
    assert out.iloc[-1] > 70.0


def test_rsi_wilder_strong_downtrend_below_30():
    rng = np.random.RandomState(2)
    deltas = rng.normal(loc=-0.8, scale=1.0, size=200)
    s = pd.Series(200 + np.cumsum(deltas))
    out = rsi_wilder(s, period=14)
    assert out.iloc[-1] < 30.0


def test_rsi_wilder_range_and_min_periods():
    """All RSI values in [0, 100]; first (period-1) values are NaN."""
    rng = np.random.RandomState(0)
    s = pd.Series(100 + rng.normal(0, 1, 100).cumsum())
    out = rsi_wilder(s, period=14)
    valid = out.dropna()
    assert (valid >= 0).all() and (valid <= 100).all()
    # min_periods=14 → at least the first 13 values must be NaN.
    assert out.iloc[:13].isna().all()


def test_sma_simple():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = sma(s, 3)
    assert out.iloc[-1] == pytest.approx(4.0)
    assert math.isnan(out.iloc[0])  # not enough data yet


def test_ema_first_value_equals_input():
    s = pd.Series([10.0, 11.0, 12.0])
    out = ema(s, 3)
    # pandas ewm adjust=False seeds with the first observation.
    assert out.iloc[0] == pytest.approx(10.0)


def test_macd_returns_three_aligned_series():
    s = pd.Series(np.linspace(100, 120, 60))
    line, sig, hist = macd(s)
    assert len(line) == len(sig) == len(hist) == len(s)
    # In a steady uptrend, MACD line is above signal → histogram positive.
    assert hist.iloc[-1] > 0


def test_bollinger_bands_ordering_and_pctb():
    s = pd.Series(np.random.RandomState(0).normal(100, 1, 200))
    upper, mid, lower, pct_b = bollinger_bands(s, period=20, std_dev=2.0)
    last = s.iloc[-1]
    assert lower.iloc[-1] < mid.iloc[-1] < upper.iloc[-1]
    expected = (last - lower.iloc[-1]) / (upper.iloc[-1] - lower.iloc[-1])
    assert pct_b.iloc[-1] == pytest.approx(expected)


def test_atr_non_negative_and_aligned():
    n = 50
    high = pd.Series(np.linspace(101, 110, n))
    low = pd.Series(np.linspace(99, 108, n))
    close = pd.Series(np.linspace(100, 109, n))
    out = atr(high, low, close, period=14)
    assert (out.dropna() >= 0).all()
    assert len(out) == n
