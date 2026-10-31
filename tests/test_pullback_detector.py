from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.strategies.pullback_detector import (
    PullbackConfig,
    detect_pullbacks,
)

ET = ZoneInfo("America/New_York")
DAY = datetime(2026, 1, 15, 9, 30, tzinfo=ET)  # session open


def bar(i, o, h, l, c, symbol="AAA", day=DAY):
    """1-minute bar i minutes after session open."""
    return SimpleNamespace(
        symbol=symbol, timestamp=day + timedelta(minutes=i),
        open=o, high=h, low=l, close=c, volume=1000,
    )


def ramp(start_i, start_price, end_price, count, symbol="AAA", day=DAY):
    """`count` bars moving linearly between prices (monotone climb)."""
    bars = []
    for k in range(count):
        p = start_price + (end_price - start_price) * k / max(count - 1, 1)
        hi = max(p, start_price + (end_price - start_price) * (k + 1) / max(count - 1, 1)) if k < count - 1 else p
        bars.append(bar(start_i + k, p, hi, p * 0.999, p, symbol, day))
    return bars


def good_pattern(symbol="AAA"):
    """Impulse 100->107 (+7%, 8 bars), pullback to ~103.8 (~53%), recovery."""
    bars = ramp(0, 100.0, 107.0, 8, symbol)
        # pullback: 4 bars down to 103.8 (~53% of 7)
    for k, p in enumerate([106.0, 105.0, 104.2, 103.8]):
        i = 8 + k
        bars.append(bar(i, p + 0.4, p + 0.5, p, p, symbol))
        # recovery: close above previous bar's high (103.8+0.5=104.3)
    bars.append(bar(12, 104.0, 104.8, 103.9, 104.5, symbol))
    return bars


def test_valid_impulse_pullback_recovery():
    patterns = detect_pullbacks(good_pattern())
    assert len(patterns) == 1
    p = patterns[0]
    assert p.state == "recovery_confirmed"
    assert p.symbol == "AAA"
    assert p.impulse_gain_pct == pytest.approx(7.0, abs=0.3)
    assert p.retracement_pct == pytest.approx(45, abs=3)
    assert p.recovery_ts is not None
    assert p.impulse_low == pytest.approx(99.9, abs=0.2)
    assert p.impulse_high == pytest.approx(107.0, abs=0.2)
    assert p.pullback_bars is not None


def test_weak_impulse_rejected():
    bars = ramp(0, 100.0, 102.0, 10)  # only +2%
    assert detect_pullbacks(bars) == []


def test_shallow_pullback_invalidated():
    bars = ramp(0, 100.0, 107.0, 8)
    for k, p in enumerate([106.5, 106.2]):  # ~10% retracement only
        bars.append(bar(8 + k, p + 0.3, p + 0.4, p, p))
    bars.append(bar(10, 106.3, 107.1, 106.2, 107.0))  # close > prev high
    patterns = detect_pullbacks(bars)
    assert patterns[0].state == "invalidated"
    assert "too shallow" in patterns[0].invalidation_reason


def test_deep_pullback_invalidated():
    bars = ramp(0, 100.0, 107.0, 8)
    for k, p in enumerate([105.0, 103.5, 102.0]):  # ~71% retracement
        bars.append(bar(8 + k, p + 0.3, p + 0.4, p, p))
    patterns = detect_pullbacks(bars)
    assert patterns[0].state == "invalidated"
    assert "retracement exceeded max" in patterns[0].invalidation_reason


def test_break_below_impulse_low_invalidates():
    bars = ramp(0, 100.0, 107.0, 8)
    bars.append(bar(8, 103.0, 103.2, 99.5, 100.0))  # below impulse low
    patterns = detect_pullbacks(bars)
    assert patterns[0].state == "invalidated"
    assert "below impulse low" in patterns[0].invalidation_reason


def test_pullback_too_long_invalidated():
    bars = ramp(0, 100.0, 107.0, 8)
    for k in range(16):  # 16 bars sideways = beyond max 15
        p = 105.0 + (k % 3) * 0.2
        bars.append(bar(8 + k, p, p + 0.3, p - 0.3, p))
    patterns = detect_pullbacks(bars)
    assert patterns[0].state == "invalidated"
    assert "max duration" in patterns[0].invalidation_reason


def test_no_recovery_close_stays_in_progress():
    bars = ramp(0, 100.0, 107.0, 8)
    for k, p in enumerate([106.0, 105.0, 104.5]):  # drifting down, never closes above prev high
        bars.append(bar(8 + k, p + 0.4, p + 0.5, p, p))
    patterns = detect_pullbacks(bars)
    assert patterns[0].state == "pullback_in_progress"
    assert patterns[0].recovery_ts is None


def test_no_duplicate_confirmations():
    bars = good_pattern()
    # Add more recovery-looking bars — belong to a *new* pattern search.
    for k, p in enumerate([105.0, 105.5, 106.0]):
        bars.append(bar(13 + k, p - 0.2, p + 0.3, p - 0.3, p))
    patterns = detect_pullbacks(bars)
    confirmed = [p for p in patterns if p.state == "recovery_confirmed"]
    assert len(confirmed) == 1
    assert confirmed[0].recovery_ts == DAY + timedelta(minutes=12)


def test_gap_splits_pattern():
    bars = ramp(0, 100.0, 107.0, 8)
    # 10-minute gap, then a "pullback" — must not stitch across the gap.
    gap_bars = [bar(20 + k, 105.0 - k, 105.2 - k, 104.5 - k, 104.8 - k) for k in range(3)]
    patterns = detect_pullbacks(bars + gap_bars, config=PullbackConfig(max_gap_minutes=3))
    assert patterns[0].state == "pullback_in_progress"


def test_session_boundary_no_stitching():
    day2 = DAY + timedelta(days=1)
    bars = ramp(0, 100.0, 107.0, 8, day=DAY)
    bars += ramp(0, 105.0, 103.5, 3, symbol="AAA", day=day2)
    patterns = detect_pullbacks(bars)
    assert patterns[0].state == "pullback_in_progress"
    assert patterns[0].impulse_start_ts.date() == DAY.date()


def test_invalid_ohlc_rejected():
    bars = [bar(0, 100.0, 99.0, 98.0, 99.5)]  # high < open
    with pytest.raises(ValueError):
        detect_pullbacks(bars)
    bars = [bar(0, 100.0, 101.0, 99.0, -5.0)]  # negative close
    with pytest.raises(ValueError):
        detect_pullbacks(bars)


def test_unsorted_or_duplicate_bars_rejected():
    bars = ramp(0, 100.0, 103.0, 3)
    with pytest.raises(ValueError):
        detect_pullbacks([bars[1], bars[0], bars[2]])  # out of order
    with pytest.raises(ValueError):
        detect_pullbacks([bars[0], bars[0], bars[1]])  # duplicate ts


def test_no_lookahead_bias():
    """Prefix detection must equal full-list detection for the same pattern."""
    full = good_pattern()
    prefix_result = detect_pullbacks(full[:13])
    full_result = detect_pullbacks(full + ramp(13, 104.5, 90.0, 5))
    assert prefix_result[0] == full_result[0]


def test_independent_patterns_after_resolution():
    """A resolved pattern frees the scan to find a later independent pattern."""
    bars = good_pattern()
    bars += ramp(13, 105.0, 112.0, 9)   # second impulse
    for k, p in enumerate([110.5, 109.5, 108.8]):  # ~46% pullback
        bars.append(bar(22 + k, p + 0.3, p + 0.4, p, p))
    bars.append(bar(25, 109.0, 109.6, 108.9, 109.4))  # recovery
    patterns = detect_pullbacks(bars)
    assert len(patterns) == 2
    assert all(p.state == "recovery_confirmed" for p in patterns)
    assert patterns[1].impulse_start_ts > patterns[0].recovery_ts


def test_config_thresholds_respected():
    cfg = PullbackConfig(min_impulse_pct=10.0)
    assert detect_pullbacks(good_pattern(), config=cfg) == []  # +7% < 10%


def test_regular_session_filter_excludes_extended_hours():
    from datetime import date, time as t
    from app.intraday_data import filter_regular_session_bars

    times = {DAY.date(): (t(9, 30), t(16, 0))}
    bars = [
        bar(0, 100, 100.5, 99.9, 100.2),   # 9:30 in-session
        bar(390, 105, 105.5, 104.9, 105.2), # 16:00 close boundary -> out
        bar(-60, 99, 99.5, 98.9, 99.2),     # 8:30 premarket -> out
        bar(420, 106, 106.5, 105.9, 106.2), # 16:30 after-hours -> out
    ]
    kept = filter_regular_session_bars(bars, times)
    assert kept == bars[:1]


def test_regular_session_filter_early_close():
    from datetime import time as t
    from app.intraday_data import filter_regular_session_bars

    times = {DAY.date(): (t(9, 30), t(13, 0))}  # early close
    inside = bar(200, 100, 100.5, 99.9, 100.2)   # 12:50 ok
    outside = bar(220, 100, 100.5, 99.9, 100.2)  # 13:10 -> out
    assert filter_regular_session_bars([inside, outside], times) == [inside]


def test_regular_session_filter_missing_calendar_day():
    from app.intraday_data import filter_regular_session_bars
    # No entry for the bar's date -> dropped conservatively.
    assert filter_regular_session_bars([bar(0, 100, 100.5, 99.9, 100.2)], {}) == []


def test_afterhours_pattern_excluded_by_session_filter():
    """Pattern that only exists in extended-hours bars is not detected."""
    from datetime import time as t
    from app.intraday_data import filter_regular_session_bars

    times = {DAY.date(): (t(9, 30), t(16, 0))}
    # Build the impulse/pullback entirely in after-hours minutes (i>=390).
    bars = ramp(390, 100.0, 107.0, 8)
    for k, p in enumerate([106.0, 105.0, 104.2, 103.8]):
        bars.append(bar(398 + k, p + 0.4, p + 0.5, p, p))
    bars.append(bar(402, 104.0, 104.8, 103.9, 104.5))

    assert detect_pullbacks(bars)  # unfiltered: pattern exists
    assert detect_pullbacks(filter_regular_session_bars(bars, times)) == []


def test_insession_pattern_survives_session_filter():
    from datetime import time as t
    from app.intraday_data import filter_regular_session_bars

    times = {DAY.date(): (t(9, 30), t(16, 0))}
    bars = good_pattern()
    assert detect_pullbacks(filter_regular_session_bars(bars, times))[0].state == "recovery_confirmed"
