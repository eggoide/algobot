"""Time / NYSE schedule helpers.

Extracted from bot.py (V5). Pure functions — no logging side-effects, no module
state beyond NYC_TZ and the NYSE calendar handle.
"""

import datetime
import sys
import time
from typing import Optional

import pytz

NYC_TZ = pytz.timezone("US/Eastern")

try:
    import pandas_market_calendars as mcal
    _NYSE_CAL = mcal.get_calendar("NYSE")
except Exception:
    _NYSE_CAL = None


def set_timezone(tz_name: str) -> None:
    global NYC_TZ
    NYC_TZ = pytz.timezone(tz_name)


def get_ny_time() -> datetime.datetime:
    return datetime.datetime.now(NYC_TZ)


def _nyse_schedule(start_day, end_day):
    if _NYSE_CAL is None:
        return None
    try:
        return _NYSE_CAL.schedule(start_date=start_day, end_date=end_day)
    except Exception:
        return None


def is_market_open() -> bool:
    ny_now = get_ny_time()
    if ny_now.weekday() > 4:
        return False
    sched = _nyse_schedule(ny_now.date(), ny_now.date())
    if sched is not None and not sched.empty:
        market_open = sched.iloc[0]["market_open"].tz_convert(NYC_TZ).to_pydatetime()
        market_close = sched.iloc[0]["market_close"].tz_convert(NYC_TZ).to_pydatetime()
        return market_open <= ny_now <= market_close
    if sched is not None and sched.empty:
        return False
    market_open = ny_now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = ny_now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= ny_now <= market_close


def seconds_until_market_open() -> int:
    ny_now = get_ny_time()
    if _NYSE_CAL is not None:
        try:
            end = (ny_now + datetime.timedelta(days=14)).date()
            sched = _NYSE_CAL.schedule(start_date=ny_now.date(), end_date=end)
            for _, row in sched.iterrows():
                m_open = row["market_open"].tz_convert(NYC_TZ).to_pydatetime()
                if m_open > ny_now:
                    return max(60, int((m_open - ny_now).total_seconds()))
        except Exception:
            pass
    today_open = ny_now.replace(hour=9, minute=30, second=0, microsecond=0)
    if ny_now < today_open and ny_now.weekday() <= 4:
        return max(60, int((today_open - ny_now).total_seconds()))
    next_day = ny_now + datetime.timedelta(days=1)
    while next_day.weekday() > 4:
        next_day += datetime.timedelta(days=1)
    next_open = next_day.replace(hour=9, minute=30, second=0, microsecond=0)
    return max(60, int((next_open - ny_now).total_seconds()))


def seconds_until(dt_target: datetime.datetime) -> int:
    now = get_ny_time()
    return max(1, int((dt_target - now).total_seconds()))


def fmt_hms(seconds: int) -> str:
    seconds = int(max(0, seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def in_sell_window(now_ny: datetime.datetime) -> bool:
    start = now_ny.replace(hour=9, minute=35, second=0, microsecond=0)
    end = now_ny.replace(hour=15, minute=55, second=59, microsecond=999999)
    return start <= now_ny <= end


def in_buy_window(now_ny: datetime.datetime) -> bool:
    if not (10 <= now_ny.hour <= 15):
        return False
    return (31 <= now_ny.minute <= 33)


def sell_cycle_id_5min(now_ny: datetime.datetime) -> str:
    bucket_min = (now_ny.minute // 5) * 5
    return f"{now_ny.strftime('%Y-%m-%d')}-{now_ny.hour:02d}-{bucket_min:02d}"


def buy_cycle_id_hour(now_ny: datetime.datetime) -> str:
    return f"{now_ny.strftime('%Y-%m-%d')}-{now_ny.hour:02d}"


def next_5min_boundary(now_ny: datetime.datetime) -> datetime.datetime:
    base = now_ny.replace(second=0, microsecond=0)
    add = 5 - (base.minute % 5)
    if add == 5 and now_ny.second == 0 and now_ny.microsecond == 0:
        add = 0
    target = base + datetime.timedelta(minutes=add)
    if target <= now_ny:
        target = target + datetime.timedelta(minutes=5)
    return target


def next_buy_run_time(now_ny: datetime.datetime) -> datetime.datetime:
    if now_ny.weekday() > 4:
        d = now_ny + datetime.timedelta(days=1)
        while d.weekday() > 4:
            d += datetime.timedelta(days=1)
        return d.replace(hour=10, minute=31, second=0, microsecond=0)
    if now_ny.hour < 10:
        return now_ny.replace(hour=10, minute=31, second=0, microsecond=0)
    if 10 <= now_ny.hour <= 15:
        run = now_ny.replace(minute=31, second=0, microsecond=0)
        if now_ny < run:
            return run
        if now_ny.hour < 15:
            return (now_ny + datetime.timedelta(hours=1)).replace(minute=31, second=0, microsecond=0)
    d = now_ny + datetime.timedelta(days=1)
    while d.weekday() > 4:
        d += datetime.timedelta(days=1)
    return d.replace(hour=10, minute=31, second=0, microsecond=0)


def next_sell_run_time(now_ny: datetime.datetime) -> datetime.datetime:
    if now_ny.weekday() > 4:
        d = now_ny + datetime.timedelta(days=1)
        while d.weekday() > 4:
            d += datetime.timedelta(days=1)
        return d.replace(hour=9, minute=35, second=0, microsecond=0)
    start = now_ny.replace(hour=9, minute=35, second=0, microsecond=0)
    end = now_ny.replace(hour=15, minute=55, second=0, microsecond=0)
    if now_ny < start:
        return start
    if now_ny > end:
        d = now_ny + datetime.timedelta(days=1)
        while d.weekday() > 4:
            d += datetime.timedelta(days=1)
        return d.replace(hour=9, minute=35, second=0, microsecond=0)
    nxt = next_5min_boundary(now_ny)
    if nxt > end:
        d = now_ny + datetime.timedelta(days=1)
        while d.weekday() > 4:
            d += datetime.timedelta(days=1)
        return d.replace(hour=9, minute=35, second=0, microsecond=0)
    return nxt
