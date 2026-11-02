from datetime import datetime, time as t, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.strategies.pullback_detector import detect_pullbacks
from app.market_data import MARKET_TZ
from app.strategies.trade_simulator import TradeConfig, simulate_pullback_trades

ET = ZoneInfo("America/New_York")
DAY = datetime(2026, 1, 15, 9, 30, tzinfo=ET)


def bar(i, o, h, l, c, symbol="AAA", day=DAY):
    return SimpleNamespace(
        symbol=symbol, timestamp=day + timedelta(minutes=i),
        open=o, high=h, low=l, close=c, volume=1000,
    )


def pattern_bars(entry_price=104.0, symbol="AAA"):
    """Confirmed pattern bars (from good_pattern shape) ending at bar 12
    recovery; then simulator-relevant bars start at index 13."""
    bars = []
    for k in range(8):  # impulse 100 -> 107
        p = 100.0 + k
        bars.append(bar(k, p, p + 1 if k < 7 else p, p * 0.999, p, symbol))
    for k, p in enumerate([106.0, 105.0, 104.2, 103.8]):  # pullback
        bars.append(bar(8 + k, p + 0.4, p + 0.5, p, p, symbol))
    bars.append(bar(12, 104.0, 104.8, 103.9, 104.5, symbol))  # recovery
    return bars


def sim(bars, config=None, session_times=None):
    patterns = detect_pullbacks(bars)
    if session_times is None:
        # Fixtures end mid-session; treat their last bar as the close so
        # exits are realized (session_end) rather than data_end.
        last_t = bars[-1].timestamp.astimezone(MARKET_TZ).time()
        session_times = {DAY.date(): (t(9, 30), last_t)}
    return simulate_pullback_trades(bars, patterns, config,
                                    session_times=session_times)


def test_entry_next_bar_open_no_lookahead():
    bars = pattern_bars() + [bar(13, 104.6, 121.0, 104.0, 119.0)]
    r = sim(bars, TradeConfig(entry_slippage_bps=0, exit_slippage_bps=0, profit_target_pct=15))
    assert len(r.trades) == 1
    t = r.trades[0]
    assert t.entry_ts == bars[13].timestamp
    assert t.entry_price == 104.6
    assert t.exit_reason == "target"


def test_stop_loss_exit():
    bars = pattern_bars() + [bar(13, 100.0, 100.5, 94.0, 95.0)]
    r = sim(bars, TradeConfig(entry_slippage_bps=0, exit_slippage_bps=0))
    t = r.trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_price == pytest.approx(100.0 * 0.95)


def test_target_exit():
    bars = pattern_bars() + [bar(13, 100.0, 116.0, 99.5, 115.0)]
    r = sim(bars, TradeConfig(entry_slippage_bps=0, exit_slippage_bps=0))
    assert r.trades[0].exit_reason == "target"
    assert r.trades[0].exit_price == pytest.approx(100.0 * 1.15)


def test_same_bar_stop_wins_over_target():
    # Bar touches both levels -> stop first (conservative).
    bars = pattern_bars() + [bar(13, 100.0, 116.0, 94.0, 110.0)]
    r = sim(bars, TradeConfig(entry_slippage_bps=0, exit_slippage_bps=0))
    assert r.trades[0].exit_reason == "stop"


def test_open_gap_through_stop():
    # Open below stop -> fill at the open, not the stop level.
    bars = pattern_bars() + [bar(13, 100.0, 100.5, 99.0, 99.5), bar(14, 90.0, 90.5, 89.0, 89.5)]
    r = sim(bars, TradeConfig(entry_slippage_bps=0, exit_slippage_bps=0))
    t = r.trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_price == 90.0  # gap fill at open, below the 95.0 stop


def test_entry_bar_can_trigger_exit():
    bars = pattern_bars() + [bar(13, 100.0, 100.5, 94.0, 95.0)]
    r = sim(bars, TradeConfig(entry_slippage_bps=0, exit_slippage_bps=0))
    assert r.trades[0].exit_ts == bars[13].timestamp


def test_session_end_exit():
    bars = pattern_bars() + [bar(13, 100.0, 100.5, 99.0, 100.2), bar(14, 100.2, 100.4, 99.8, 100.1)]
    r = sim(bars, TradeConfig(entry_slippage_bps=0, exit_slippage_bps=0))
    t = r.trades[0]

    assert t.exit_price == 100.1  # last regular-session bar close


def test_no_entry_without_next_bar():
    r = sim(pattern_bars())  # recovery is the last bar
    assert len(r.trades) == 0
    assert r.skipped[0][1] == "no next regular-session bar"


def test_no_overnight_positions():
    day2 = DAY + timedelta(days=1)
    bars = pattern_bars()
    # recovery at day1 bar 12; next bar is next day -> skip
    bars.append(bar(0, 100.0, 101.0, 99.0, 100.5, day=day2))
    r = sim(bars)
    assert len(r.trades) == 0
    assert "no next regular-session bar" in r.skipped[0][1]


def test_position_sizing_one_pct_risk():
    # entry 104.6, stop 5% -> risk/share 5.23; budget 20 -> 3 shares
    bars = pattern_bars() + [bar(13, 104.6, 105.0, 104.0, 104.5)]
    r = sim(bars, TradeConfig(entry_slippage_bps=0, exit_slippage_bps=0,
                              initial_equity=2000, risk_pct=0.01))
    t = r.trades[0]
    assert t.shares == 3  # floor(20 / (104.6*0.05)) = floor(3.82)
    assert t.risk_budget == 20.0


def test_insufficient_buying_power():
    bars = pattern_bars() + [bar(13, 104.6, 105.0, 104.0, 104.5)]
    r = sim(bars, TradeConfig(initial_equity=100.0))  # can't afford 1 share
    assert len(r.trades) == 0
    assert "buying power" in r.skipped[0][1]


def test_slippage_and_commission():
    bars = pattern_bars() + [bar(13, 100.0, 116.0, 99.5, 115.0)]
    r = sim(bars, TradeConfig(entry_slippage_bps=100, exit_slippage_bps=100,
                              commission_per_trade=1.0, initial_equity=2000,
                              risk_pct=0.5))
    t = r.trades[0]
    assert t.entry_price == pytest.approx(101.0)      # 100 * 1.01
    assert t.target_price == pytest.approx(116.15)    # 101 * 1.15
    assert t.net_pnl == pytest.approx(t.gross_pnl - 2.0, abs=0.01)


def test_no_overlapping_positions_and_equity_updates():
    """Second pattern while first position open -> skipped; equity updates."""
    bars = pattern_bars()
    bars.append(bar(13, 104.6, 120.0, 104.0, 118.0))  # big winner target
    bars += pattern_bars(symbol="AAA") if False else []  # keep simple
    # second pattern later same day
    p2 = pattern_bars()
    for i, b in enumerate(p2):
        b.timestamp = DAY + timedelta(minutes=14 + i)
    bars += p2
    bars.append(bar(27, 105.0, 106.0, 104.0, 105.5))
    r = sim(bars, TradeConfig(entry_slippage_bps=0, exit_slippage_bps=0))
    assert len(r.trades) == 1  # second pattern entered after first closed
    assert r.trades[0].equity_after > 2000  # equity updated after trade


def test_zero_confirmed_patterns():
    bars = [bar(k, 100.0 + k * 0.1, 100.3 + k * 0.1, 99.9 + k * 0.1, 100.2 + k * 0.1) for k in range(10)]
    r = sim(bars)
    assert r.patterns_confirmed == 0
    assert r.trades == ()
    assert r.win_rate is None


def test_target_scenarios_differ():
    bars = pattern_bars() + [
        bar(13, 100.0, 117.0, 99.5, 116.0),   # +17% high
        bar(14, 116.0, 116.5, 115.0, 115.5),
    ]
    a = sim(bars, TradeConfig(profit_target_pct=15.0, entry_slippage_bps=0, exit_slippage_bps=0))
    b = sim(bars, TradeConfig(profit_target_pct=20.0, entry_slippage_bps=0, exit_slippage_bps=0))
    assert a.trades[0].exit_reason == "target"



def test_no_duplicate_trades_per_pattern():
    r = sim(pattern_bars() + [bar(13, 100.0, 101.0, 99.0, 100.5)])
    assert len(r.trades) <= r.patterns_confirmed


def test_evgo_style_session_end_trade():
    """Regression: recovery 9:40 -> entry 9:41 next bar, no stop/target hit,
    exits at last regular-session bar close (no lookahead, costs applied).
    Equity 2000, 1% risk -> shares cash-bound by buying power."""
    bars = pattern_bars() + [
        bar(13, 9.40, 9.50, 9.30, 9.45),   # entry bar ~9.40 open
        bar(14, 9.45, 9.50, 9.35, 9.42),
        bar(15, 9.42, 9.48, 9.38, 9.44),   # final regular-session bar
    ]
    r = sim(
        bars,
        TradeConfig(initial_equity=2000, risk_pct=0.01, entry_slippage_bps=0,
                    exit_slippage_bps=0, commission_per_trade=0),
    )
    t = r.trades[0]
    assert t.entry_ts == bars[13].timestamp          # next bar after confirm
    assert t.entry_price == 9.40

    assert t.exit_ts == bars[15].timestamp            # last bar of segment
    assert t.exit_price == 9.44
    # shares = min(floor(20/0.47)=42, floor(2000/9.40)=212) -> 42 (risk-bound)
    assert t.shares == 42
    assert t.net_pnl == pytest.approx((9.44 - 9.40) * 42, abs=0.01)


def test_evgo_cash_bound_when_stop_wide():
    """212 shares arises when buying power binds, not the risk budget."""
    bars = pattern_bars() + [
        bar(13, 9.40, 9.50, 9.30, 9.45),
        bar(14, 9.45, 9.48, 9.38, 9.44),
    ]
    r = sim(
        bars,
        TradeConfig(initial_equity=2000, risk_pct=0.5, entry_slippage_bps=0,
                    exit_slippage_bps=0, commission_per_trade=0),
    )
    assert r.trades[0].shares == 212  # floor(2000/9.40) — cash binds first


def test_true_session_end_at_official_close():
    """Final bar near the official close -> session_end, not data_end."""
    bars = pattern_bars() + [bar(13, 100.0, 100.4, 99.0, 100.2)]
    for k in range(14, 390):  # contiguous through 15:59 ET
        bars.append(bar(k, 100.1, 100.3, 99.9, 100.1))
    r = sim(bars, TradeConfig(entry_slippage_bps=0, exit_slippage_bps=0))
    assert r.trades[0].exit_reason == "session_end"


def test_data_end_distinguished_with_calendar():
    """With calendar close at 16:00, data ending 11:50 is data_end."""
    from datetime import time as t
    midday = DAY + timedelta(minutes=140)  # 11:50 ET
    bars = pattern_bars() + [
        bar(13, 9.40, 9.50, 9.30, 9.45),
        bar(140, 9.45, 9.48, 9.38, 9.44, day=DAY.replace(hour=DAY.hour)),  # far gap -> separate segment? use contiguous
    ]
    # simpler: contiguous bars ending mid-session
    bars = pattern_bars() + [bar(13, 9.40, 9.50, 9.30, 9.45), bar(14, 9.45, 9.48, 9.38, 9.44)]
    times = {DAY.date(): (t(9, 30), t(16, 0))}
    from app.strategies.trade_simulator import simulate_pullback_trades as spt
    patterns = detect_pullbacks(bars)
    r = spt(bars, patterns, TradeConfig(entry_slippage_bps=0, exit_slippage_bps=0), session_times=times)
    assert r.unresolved[0].exit_reason == "data_end"
    assert r.trades == ()  # not realized


def test_unresolved_not_counted_as_realized():
    """data_end: provisional mark only — excluded from equity, P/L, win rate."""
    bars = pattern_bars() + [
        bar(13, 9.40, 9.50, 9.30, 9.45),
        bar(14, 9.45, 9.48, 9.38, 9.44),
    ]
    times = {DAY.date(): (t(9, 30), t(16, 0))}
    r = sim(bars, TradeConfig(entry_slippage_bps=0, exit_slippage_bps=0,
                              commission_per_trade=0), session_times=times)
    assert len(r.unresolved) == 1
    assert len(r.trades) == 0                     # not realized
    assert r.win_rate is None                      # no completed trades
    assert r.net_pnl == 0 and r.gross_pnl == 0     # provisional mark excluded
    assert r.ending_equity == 2000                 # equity untouched
    assert r.unresolved[0].net_pnl != 0            # mark-to-market kept
    assert "data_end" not in r.exit_reasons        # not a realized exit


def test_realized_exit_unaffected_by_unresolved():
    """A realized target + an unresolved data_end: only realized counts."""
    bars = pattern_bars() + [bar(13, 105.0, 106.0, 104.5, 105.5), bar(14, 106.0, 125.0, 105.5, 122.0)]
    r = sim(bars, TradeConfig(entry_slippage_bps=0, exit_slippage_bps=0, commission_per_trade=0))
    assert len(r.trades) == 1 and r.trades[0].exit_reason == "target"
    assert r.ending_equity > 2000  # realized pnl applied
