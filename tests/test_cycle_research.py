from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.cycle_research import count_cycles, daily_ranges, detect_legs

ET = ZoneInfo("America/New_York")
DAY = datetime(2026, 5, 4, 9, 30, tzinfo=ET)  # Monday


def bars_for(prices, symbol="XXX"):
    return [
        SimpleNamespace(
            symbol=symbol, timestamp=DAY + timedelta(minutes=i),
            open=p, high=p * 1.001, low=p * 0.999, close=p, volume=1000,
        )
        for i, p in enumerate(prices)
    ]


def test_simple_up_down_cycle():
    # up-leg confirmed on the drop below 99.75; down-leg confirmed on the
    # subsequent +5% rise -> one complete cycle.
    prices = [100, 105, 99.5, 105]
    cycles = count_cycles(bars_for(prices), 5.0)
    assert len(cycles) == 1
    c = cycles[0]
    assert c.pivot_price > c.start_price
    assert c.amplitude_pct == pytest.approx(10.24, abs=0.05)


def test_two_oscillations_two_cycles():
    # four confirmed legs -> two non-overlapping cycles
    prices = [100, 106, 100, 106, 100, 106]
    cycles = count_cycles(bars_for(prices), 5.0)
    assert len(cycles) == 2


def test_sub_threshold_moves_ignored():
    prices = [100, 102, 100, 102, 100]  # 2% wiggles only
    assert count_cycles(bars_for(prices), 5.0) == []


def test_non_overlapping_cycles():
    # Three alternating 10% legs -> exactly 1 cycle, legs not reused.
    prices = [100, 110, 99, 109]
    cycles = count_cycles(bars_for(prices), 8.0)
    assert len(cycles) == 1


def test_incomplete_leg_not_counted():
    prices = [100, 106, 104]  # up-leg confirmed but no second leg
    assert count_cycles(bars_for(prices), 5.0) == []


def test_no_lookahead_confirmation_lag():
    prices = [100, 101, 102, 103, 104, 105, 103, 101, 99, 96]
    legs = detect_legs(bars_for(prices), 5.0)
    # Up leg pivot at index 5 (105) is only confirmed when price falls 5%:
    # confirm at first close <= 99.75 -> index 9 (96)? 105*0.95=99.75 -> idx9.
    assert legs[0][0].pivot_price == 105
    # 99 <= 105*0.95 = 99.75 -> confirmed at index 8, not at the pivot.
    assert legs[0][0].confirm_ts == DAY + timedelta(minutes=8)


def test_daily_ranges_separate_metric():
    bars = bars_for([100, 110, 90, 100])
    ranges = daily_ranges(bars)
    d = DAY.date().isoformat()
    # bars carry hi=p*1.001 / lo=p*0.999 -> (110.11-89.91)/89.91 ≈ 22.47%
    assert ranges[d] == pytest.approx(22.47, abs=0.05)
