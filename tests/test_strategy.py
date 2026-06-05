import pytest

from strategy import EnhancedDipBuyStrategy


def _make(params=None):
    base = dict(
        sell_gain=0.03,
        use_stop_loss=True, stop_loss=0.07,
        use_trailing_stop=True, trailing_stop_pct=0.02,
        use_time_stop=True, time_stop_bars=120,
        use_macd=False, use_bollinger=False, use_volume_filter=False, use_sma_filter=False,
    )
    if params:
        base.update(params)
    return EnhancedDipBuyStrategy(base)


def test_should_exit_take_profit():
    s = _make()
    out = s.should_exit("AAPL", entry_price=100.0, current_price=103.5, holding_bars=10)
    assert out is not None and "Take Profit" in out.reason


def test_should_exit_stop_loss():
    s = _make()
    out = s.should_exit("AAPL", entry_price=100.0, current_price=92.5, holding_bars=10)
    assert out is not None and "Stop Loss" in out.reason


def test_should_exit_trailing_stop_only_in_profit():
    s = _make()
    # Seed HWM directly. (should_exit only writes HWM when current > stored, and
    # default-stored == current on the first call, so HWM seeding never happens
    # implicitly — callers / state restore must populate it.)
    s._high_water_marks["AAPL"] = 102.0
    # Price drops 2.5% from HWM and we are still slightly underwater vs entry,
    # but trailing stop should fire as soon as pnl crossed positive once. Move to
    # entry+1% then drop to trigger.
    s._high_water_marks["AAPL"] = 102.0
    out = s.should_exit("AAPL", 100.0, 99.5, 6)
    # pnl_pct < 0 here → trailing stop check requires pnl > 0; should be None.
    assert out is None
    # Now still in profit (101 vs entry 100) but >2% below HWM 105 → triggers.
    s._high_water_marks["AAPL"] = 105.0
    out = s.should_exit("AAPL", 100.0, 101.0, 7)
    assert out is not None and "Trailing Stop" in out.reason


def test_should_exit_trailing_inactive_when_underwater():
    s = _make()
    # Entry 100, price went straight down to 99 then to 95 → trailing must NOT fire
    # because pnl_pct never went positive. Only stop_loss can fire (here -5%, not yet).
    s.should_exit("AAPL", 100.0, 99.0, 1)
    out = s.should_exit("AAPL", 100.0, 95.0, 2)
    assert out is None


def test_should_exit_time_stop():
    s = _make({"time_stop_bars": 5})
    out = s.should_exit("AAPL", 100.0, 100.5, holding_bars=5)
    assert out is not None and "Time Stop" in out.reason


def test_should_exit_priority_take_profit_over_time_stop():
    s = _make({"time_stop_bars": 1})
    out = s.should_exit("AAPL", 100.0, 105.0, holding_bars=10)
    assert "Take Profit" in out.reason


def test_should_exit_entry_zero_returns_none():
    s = _make()
    assert s.should_exit("AAPL", entry_price=0.0, current_price=100.0, holding_bars=1) is None


def test_should_exit_clears_hwm_on_exit():
    s = _make()
    s._high_water_marks["AAPL"] = 102.0  # seed (see comment in trailing test)
    s.should_exit("AAPL", 100.0, 103.5, 2)  # take profit → must clear HWM
    assert "AAPL" not in s._high_water_marks
