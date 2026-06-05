# Tests

Run from repo root:

```
python3 -m pytest tests/ -q
```

`conftest.py` adds `app/` to `sys.path` so tests can `from indicators import ...`
without installing anything.

`tests/test_indicators.py` — RSI, MACD, BB, ATR sanity / range checks.
`tests/test_strategy.py` — `EnhancedDipBuyStrategy.should_exit` branches
(take profit, stop loss, trailing stop, time stop, edge cases).

Note: trailing-stop tests seed `_high_water_marks` manually because
`should_exit` only writes HWM when `current_price > stored`, and the default
seed equals `current_price` on the first call, so HWM seeding never happens
implicitly. Callers (or state restore) must populate it.
