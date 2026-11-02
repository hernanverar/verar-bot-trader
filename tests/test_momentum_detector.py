from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.strategies.momentum_detector import (
    MomentumSetupConfig, detect_momentum_setups,
)

ET = ZoneInfo("America/New_York")
DAY = datetime(2026, 1, 15, 9, 30, tzinfo=ET)
CFG = MomentumSetupConfig()


def bar(i, o, h, l, c, v=1000, day=DAY):
    return SimpleNamespace(timestamp=day + timedelta(minutes=i),
                           open=o, high=h, low=l, close=c, volume=v)


def impulse_bars(base=2.0):
    """Bars 0..4 rising +6% from low 2.0."""
    return [
        bar(0, 2.00, 2.02, 1.98, 2.00),
        bar(1, 2.00, 2.05, 1.99, 2.04),
        bar(2, 2.04, 2.09, 2.02, 2.08),
        bar(3, 2.08, 2.13, 2.06, 2.12),   # >= 5% from 1.98 -> IMPULSE
    ]


def test_valid_setup_confirm():
    bars = impulse_bars() + [
        bar(4, 2.12, 2.13, 2.07, 2.08, v=500),   # pullback bar1
        bar(5, 2.08, 2.09, 2.06, 2.06, v=400),   # pullback bar2 (depth 46%)
        bar(6, 2.06, 2.16, 2.06, 2.15, v=1200),  # breakout close>2.13, vol>1.5x450
    ]
    setups, rej = detect_momentum_setups(bars, CFG, "EVGO")
    assert len(setups) == 1
    s = setups[0]
    assert s.pending_entry is True
    assert s.confirm_price == 2.15
    assert s.stop_price == 2.06            # pullback low
    assert s.impulse_high == 2.13
    assert s.pullback_bars == 2
    assert rej == []


def test_impulse_threshold_boundary():
    # high = 1.98 * 1.05 = 2.079 -> just under 5% = no impulse
    bars = [bar(0, 2.0, 2.01, 1.98, 2.0), bar(1, 2.0, 2.079, 1.99, 2.07),
            bar(2, 2.07, 2.12, 2.06, 2.11)]
    setups, _ = detect_momentum_setups(bars, CFG)
    assert setups == []
    # exactly 5% (2.079) qualifies? 1.98*1.05=2.079 -> high must be >=
    bars2 = [bar(0, 2.0, 2.01, 1.98, 2.0), bar(1, 2.0, 2.079, 1.99, 2.07)]
    setups2, rej2 = detect_momentum_setups(bars2, CFG)
    assert setups2 == [] and rej2[-1].reason == "impulse_no_pullback_data"


def test_pullback_too_deep_rejected():
    bars = impulse_bars() + [bar(4, 2.12, 2.12, 2.0, 2.01, v=500)]
    # range = 2.13-1.98 = 0.15; low 2.0 -> depth (2.13-2.0)/0.15 = 86% > 50%
    setups, rej = detect_momentum_setups(bars, CFG)
    assert setups == [] and rej[-1].reason == "breakdown_below_anchor"


def test_pullback_too_long_rejected():
    bars = impulse_bars() + [bar(4 + i, 2.10, 2.11, 2.06, 2.08, v=400) for i in range(10)]
    _, rej = detect_momentum_setups(bars, CFG)
    assert any(r.reason == "pullback_too_long" for r in rej)


def test_breakout_needs_volume():
    bars = impulse_bars() + [
        bar(4, 2.12, 2.13, 2.08, 2.09, v=1000),
        bar(5, 2.09, 2.16, 2.08, 2.15, v=100),   # breakout close>2.13 but vol<1.5x
    ]
    setups, rej = detect_momentum_setups(bars, CFG)
    assert setups == [] and rej[-1].reason == "low_breakout_volume"


def test_gap_invalidates():
    bars = impulse_bars() + [
        bar(4, 2.12, 2.13, 2.08, 2.09, v=500),
        bar(20, 2.10, 2.15, 2.09, 2.14, v=2000),  # 16-min gap
    ]
    _, rej = detect_momentum_setups(bars, CFG)
    assert any(r.reason == "gap_or_session_boundary" for r in rej)


def test_empty_and_missing_bars():
    setups, rej = detect_momentum_setups([], CFG)
    assert setups == [] and rej == []


def test_determinism():
    bars = impulse_bars() + [
        bar(4, 2.12, 2.13, 2.07, 2.08, v=500),
        bar(5, 2.08, 2.09, 2.05, 2.06, v=400),
        bar(6, 2.06, 2.16, 2.05, 2.15, v=1200),
    ]
    a = detect_momentum_setups(bars, CFG, "X")
    b = detect_momentum_setups(bars, CFG, "X")
    assert a == b


def test_pending_entry_not_trade():
    setups, _ = detect_momentum_setups(
        impulse_bars() + [bar(4, 2.12, 2.13, 2.08, 2.09, v=500),
                          bar(5, 2.09, 2.16, 2.08, 2.15, v=1200)], CFG)
    s = setups[0]
    assert s.pending_entry is True
    assert not hasattr(s, "exit_price")


def test_multiple_setups_sequential():
    bars = impulse_bars() + [
        bar(4, 2.12, 2.13, 2.08, 2.09, v=500),
        bar(5, 2.09, 2.16, 2.08, 2.15, v=1200),  # confirm #1
        # second impulse from 2.15
        bar(6, 2.15, 2.28, 2.14, 2.26, v=1500),  # new impulse ~+6%
        bar(7, 2.26, 2.27, 2.22, 2.24, v=400),   # pullback (43% depth)
        bar(8, 2.22, 2.32, 2.21, 2.30, v=1200),  # confirm #2
    ]
    setups, _ = detect_momentum_setups(bars, CFG)
    assert len(setups) == 2


def test_pullback_depth_at_boundary():
    # range 0.15 -> 50% = 0.075 below 2.13 -> low 2.055 allowed
    ok = impulse_bars() + [bar(4, 2.12, 2.12, 2.055, 2.08, v=500),
                            bar(5, 2.08, 2.16, 2.07, 2.15, v=1200)]
    setups, _ = detect_momentum_setups(ok, CFG)
    assert len(setups) == 1


def test_sideways_bar_stays_impulse_no_pullback():
    """Equal close with high <= impulse_high is NOT contraction -> no pullback."""
    bars = impulse_bars() + [
        bar(4, 2.12, 2.13, 2.11, 2.12, v=500),   # same close as prev — sideways
        bar(5, 2.12, 2.13, 2.11, 2.12, v=400),   # still sideways
        bar(6, 2.12, 2.16, 2.06, 2.15, v=1200),  # breakout (no pullback needed? count=0)
    ]
    setups, rej = detect_momentum_setups(bars, CFG)
    # Sideways bars didn't start pullback; bar6 high>impulse_high extends it.
    assert setups == []
    assert not any(r.reason == "pullback_too_long" for r in rej)


def test_genuine_decline_starts_pullback():
    bars = impulse_bars() + [
        bar(4, 2.12, 2.13, 2.07, 2.08, v=500),   # close 2.08 < 2.12 -> real contraction
        bar(5, 2.08, 2.16, 2.07, 2.15, v=1200),
    ]
    setups, _ = detect_momentum_setups(bars, CFG)
    assert len(setups) == 1 and setups[0].pullback_bars == 1


def test_no_duplicate_setup_same_impulse():
    """After CONFIRMED, prior impulse bars must not re-anchor a setup."""
    bars = impulse_bars() + [
        bar(4, 2.12, 2.13, 2.08, 2.09, v=500),
        bar(5, 2.09, 2.16, 2.08, 2.15, v=1200),  # confirm #1 at ~2.15
        bar(6, 2.15, 2.16, 2.14, 2.15, v=300),   # flat — must NOT re-detect old impulse
    ]
    setups, _ = detect_momentum_setups(bars, CFG)
    assert len(setups) == 1   # not 2 from reused anchor


def test_two_genuine_setups_distinct_anchors():
    bars = impulse_bars() + [
        bar(4, 2.12, 2.13, 2.08, 2.09, v=500),
        bar(5, 2.09, 2.16, 2.08, 2.15, v=1200),  # setup 1 confirms
        bar(6, 2.15, 2.15, 2.10, 2.11, v=300),   # new low zone 2.10
        bar(7, 2.11, 2.12, 2.09, 2.10, v=300),   # anchor dips to 2.09
        bar(8, 2.10, 2.25, 2.10, 2.23, v=1500),  # NEW impulse from 2.09 anchor
        bar(9, 2.23, 2.24, 2.18, 2.19, v=400),   # pullback
        bar(10, 2.19, 2.30, 2.18, 2.28, v=1400), # confirm #2
    ]
    setups, _ = detect_momentum_setups(bars, CFG)
    assert len(setups) == 2
    assert setups[0].anchor_low != setups[1].anchor_low  # distinct anchors


# ---- Step 2: simulator integration ----
from app.strategies.momentum_simulator import simulate_momentum_setups
from app.strategies.trade_simulator import TradeConfig

SIM_CFG = TradeConfig(entry_slippage_bps=0, exit_slippage_bps=0,
                      commission_per_trade=0, risk_reward=2.0)


def setup_bars():
    """Impulse + pullback + breakout + follow-through, all 1m."""
    return impulse_bars() + [
        bar(4, 2.12, 2.13, 2.07, 2.08, v=500),     # pullback (low 2.07)
        bar(5, 2.08, 2.16, 2.07, 2.15, v=1200),    # breakout confirm at bar5
        bar(6, 2.15, 2.20, 2.14, 2.18, v=900),     # entry bar: open 2.15
        bar(7, 2.18, 2.30, 2.17, 2.28, v=800),     # target zone (stop 2.07)
        bar(8, 2.28, 2.29, 2.25, 2.26, v=700),     # last bar
    ]


def test_entry_next_bar_no_same_bar():
    bars = setup_bars()
    setups, _ = detect_momentum_setups(bars, CFG, "EVGO")
    r = simulate_momentum_setups(bars, setups, SIM_CFG)
    t = r.unresolved[0]                            # data_end -> unresolved
    assert t.exit_reason == "data_end"
    assert t.entry_ts == bars[6].timestamp       # bar AFTER confirm (bar5)
    assert t.entry_price == 2.15                  # next-bar open


def test_no_entry_when_no_next_bar():
    bars = setup_bars()[:6]                       # data ends at confirm bar
    setups, _ = detect_momentum_setups(bars, CFG, "EVGO")
    r = simulate_momentum_setups(bars, setups, SIM_CFG)
    assert r.trades == () and len(r.unresolved) == 0
    assert any("no next" in reason for _, reason in r.skipped)


def test_stop_and_2r_target_from_entry():
    bars = setup_bars()
    setups, _ = detect_momentum_setups(bars, CFG, "EVGO")
    r = simulate_momentum_setups(bars, setups, SIM_CFG)
    t = r.unresolved[0]
    # entry 2.15, stop = pullback low 2.07 -> risk 0.08 -> target 2.31
    assert t.stop_price == 2.07
    assert t.target_price == pytest.approx(2.15 + 2 * 0.08, abs=0.001)


def test_realized_exit_with_session_close():
    """Session_times close at fixture end -> realized session_end trade."""
    from datetime import time as dtime
    bars = setup_bars()
    setups, _ = detect_momentum_setups(bars, CFG, "EVGO")
    times = {DAY.date(): (dtime(9, 30), dtime(9, 39))}  # early close at last bar
    r = simulate_momentum_setups(bars, setups, SIM_CFG, session_times=times)
    assert len(r.trades) == 1
    assert r.trades[0].exit_reason in ("session_end", "target", "stop")


def test_sim_determinism_repeat():
    bars = setup_bars()
    setups, _ = detect_momentum_setups(bars, CFG, "EVGO")
    r1 = simulate_momentum_setups(bars, setups, SIM_CFG)
    r2 = simulate_momentum_setups(bars, setups, SIM_CFG)
    assert r1.trades == r2.trades


def test_gap_open_below_stop_skipped():
    """Entry bar opening at/below stop -> skipped, no favorable fill."""
    bars = impulse_bars() + [
        bar(4, 2.12, 2.13, 2.07, 2.08, v=500),
        bar(5, 2.08, 2.16, 2.07, 2.15, v=1200),  # confirm (stop 2.07)
        bar(6, 2.05, 2.10, 2.00, 2.02, v=900),   # opens BELOW stop 2.07
    ]
    setups, _ = detect_momentum_setups(bars, CFG, "EVGO")
    r = simulate_momentum_setups(bars, setups, SIM_CFG)
    assert r.trades == () and r.unresolved == ()
    assert any("risk geometry" in reason for _, reason in r.skipped)


def test_no_fill_before_next_bar_invariant():
    """Confirm bar itself can never produce an entry fill."""
    bars = impulse_bars() + [
        bar(4, 2.12, 2.13, 2.08, 2.09, v=500),
        bar(5, 2.09, 2.16, 2.08, 2.15, v=1200),  # confirm bar5
        bar(6, 2.15, 2.20, 2.14, 2.18, v=900),   # first eligible entry bar
    ]
    setups, _ = detect_momentum_setups(bars, CFG, "EVGO")
    r = simulate_momentum_setups(bars, setups, SIM_CFG)
    assert r.unresolved[0].entry_ts == bars[6].timestamp
    assert r.unresolved[0].entry_ts != bars[5].timestamp


def test_lifecycle_mapping():
    from types import SimpleNamespace as NS
    from app.strategies.momentum_simulator import lifecycle_status
    ts = DAY + timedelta(minutes=1)
    sim = NS(trades=[], unresolved=[], skipped=[])
    assert lifecycle_status(ts, sim) == "confirmed_pending_entry"
    sim.skipped = [(ts, "no next regular-session bar")]
    assert lifecycle_status(ts, sim) == "skipped_no_next_bar"
    sim.skipped = [(ts, "invalid risk geometry (stop/target nonpositive)")]
    assert lifecycle_status(ts, sim) == "skipped_invalid_risk"
    sim.unresolved = [NS(recovery_ts=ts)]
    assert lifecycle_status(ts, sim) == "unresolved_data_end"
    sim.trades = [NS(recovery_ts=ts)]
    assert lifecycle_status(ts, sim) == "simulated_filled"
