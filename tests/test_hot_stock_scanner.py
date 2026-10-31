from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.intraday_data import completed_minute_bars
from app.market_session import get_market_session
from app.strategies.hot_stock_scanner import (
    ScannerConfig,
    scan_hot_stocks,
)

ET = ZoneInfo("America/New_York")
# Thursday 11:00 ET, mid-session.
NOW = datetime(2026, 1, 15, 11, 0, tzinfo=ET)
TODAY = NOW.date()
PRIOR_DAYS = [date(2026, 1, 12), date(2026, 1, 13), date(2026, 1, 14)]

TRADING_DAYS = {
    d: (time(9, 30), time(16, 0)) for d in [TODAY, *PRIOR_DAYS]
}


def verified_session(now=NOW):
    """Calendar-verified session info — without it no test can be eligible."""
    return get_market_session(now=now, trading_days=TRADING_DAYS)


@dataclass
class FakeBar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class FakeQuote:
    symbol: str
    timestamp: datetime
    bid_price: float
    ask_price: float


class FakeBarSet:
    def __init__(self, data):
        self.data = data


class FakeClient:
    def __init__(self, bars_by_symbol, quotes=None):
        self.bars_by_symbol = bars_by_symbol
        self.quotes = quotes or {}
        self.bars_request = None

    def get_stock_bars(self, request_params):
        self.bars_request = request_params
        return FakeBarSet(self.bars_by_symbol)

    def get_stock_latest_quote(self, request_params):
        return self.quotes


def minute_bars(symbol, day, count, start_price, end_price, per_bar_volume):
    """`count` consecutive 1-minute bars from 9:30 ET with linear price path."""
    start_ts = datetime.combine(day, time(9, 30), tzinfo=ET)
    bars = []
    for i in range(count):
        price = start_price + (end_price - start_price) * i / max(count - 1, 1)
        bars.append(
            FakeBar(
                symbol,
                start_ts + timedelta(minutes=i),
                price,
                price * 1.001,
                price * 0.999,
                price,
                per_bar_volume,
            )
        )
    return bars


def hot_day_bars(symbol):
    """90 bars today, flat $100 -> $103, 3x prior-day volume."""
    bars = []
    for d in PRIOR_DAYS:
        bars += minute_bars(symbol, d, 90, 100.0, 100.0, 1_000)
    bars += minute_bars(symbol, TODAY, 90, 100.0, 103.0, 3_000)
    return bars


def test_completed_minute_filter_drops_unfinished_bar():
    bars = minute_bars("X", TODAY, 91, 100.0, 100.0, 1_000)
    completed = completed_minute_bars(bars, now=NOW)
    assert len(completed) == 90
    assert completed[-1].timestamp.time() == time(10, 59)


def test_scan_computes_change_rvol_and_score():
    bars = hot_day_bars("AAA")
    quotes = {"AAA": FakeQuote("AAA", NOW, 102.9, 103.1)}
    results = scan_hot_stocks(["AAA"], client=FakeClient({"AAA": bars}, quotes), now=NOW, session_info=verified_session(NOW))

    stock = results[0]
    assert stock.classification == "eligible"
    assert stock.reasons == ()
    assert stock.bar_freshness == "fresh"
    assert stock.quote_available is True
    assert stock.change_pct == pytest.approx(0.03, abs=1e-4)
    # Today: 270k by 11:00 vs 90k/day average at the same time -> 3x.
    assert stock.relative_volume == pytest.approx(3.0)
    assert stock.cum_volume == 270_000
    assert stock.dollar_volume > 0
    assert stock.momentum_pct is not None
    assert stock.spread_bps == pytest.approx(19.4, abs=0.1)
    assert 0 < stock.score <= 100
    assert stock.as_of.time() == time(10, 59)


def test_scan_reports_unavailable_symbol():
    results = scan_hot_stocks(
        ["AAA", "ZZZ"],
        client=FakeClient({"AAA": hot_day_bars("AAA")}, {"AAA": FakeQuote("AAA", NOW, 102.9, 103.1)}),
        now=NOW, session_info=verified_session(NOW),
    )
    by_symbol = {r.symbol: r for r in results}
    assert by_symbol["ZZZ"].classification == "unavailable"
    assert by_symbol["ZZZ"].reasons == ("no intraday bars",)
    assert by_symbol["ZZZ"].bar_freshness == "none"
    assert results[0].symbol == "AAA"  # eligible sorts first


def test_declining_stock_is_filtered_not_eligible():
    # Down ~4% today: evaluated but fails the +3% requirement.
    bars = []
    for d in PRIOR_DAYS:
        bars += minute_bars("AAA", d, 90, 100.0, 100.0, 1_000)
    bars += minute_bars("AAA", TODAY, 90, 100.0, 96.0, 3_000)
    quotes = {"AAA": FakeQuote("AAA", NOW, 95.9, 96.1)}
    results = scan_hot_stocks(["AAA"], client=FakeClient({"AAA": bars}, quotes), now=NOW, session_info=verified_session(NOW))

    stock = results[0]
    assert stock.classification == "filtered"
    assert any("below +3.00% minimum" in r for r in stock.reasons)
    assert stock.score is not None  # still evaluated, just not eligible


def test_stale_and_missing_baseline_flagged_independently():
    # Only today's bars, ending an hour before `now`.
    bars = minute_bars("AAA", TODAY, 30, 100.0, 104.0, 5_000)
    results = scan_hot_stocks(["AAA"], client=FakeClient({"AAA": bars}), now=NOW, session_info=verified_session(NOW))
    stock = results[0]
    assert stock.classification == "filtered"
    assert stock.bar_freshness == "stale"
    assert stock.quote_available is False
    assert any(r.startswith("stale bars") for r in stock.reasons)
    assert any("insufficient baseline" in r for r in stock.reasons)
    assert "no usable quote" in stock.reasons
    assert stock.relative_volume is None


def test_stale_ok_when_freshness_not_required():
    # Prior days for baseline + quote, but today's bars end 16 min ago.
    bars = []
    for d in PRIOR_DAYS:
        bars += minute_bars("AAA", d, 90, 100.0, 100.0, 1_000)
    bars += minute_bars("AAA", TODAY, 75, 100.0, 104.0, 3_000)
    quotes = {"AAA": FakeQuote("AAA", NOW, 103.9, 104.1)}

    strict = scan_hot_stocks(["AAA"], client=FakeClient({"AAA": bars}, quotes), now=NOW, session_info=verified_session(NOW))
    assert strict[0].classification == "filtered"
    assert any(r.startswith("stale bars") for r in strict[0].reasons)

    relaxed = scan_hot_stocks(
        ["AAA"],
        client=FakeClient({"AAA": bars}, quotes),
        config=ScannerConfig(require_fresh_data=False),
        now=NOW, session_info=verified_session(NOW),
    )
    assert relaxed[0].classification == "eligible"


def test_min_rvol_gate_filters_low_volume_stock():
    # Prior days heavy, today light -> rvol < 1.
    bars = []
    for d in PRIOR_DAYS:
        bars += minute_bars("AAA", d, 90, 100.0, 100.0, 50_000)
    bars += minute_bars("AAA", TODAY, 90, 100.0, 103.0, 5_000)
    config = ScannerConfig(min_rvol=1.5)
    results = scan_hot_stocks(["AAA"], client=FakeClient({"AAA": bars}), config=config, now=NOW, session_info=verified_session(NOW))
    assert results[0].classification == "filtered"
    assert any(r.startswith("rvol") and "below" in r for r in results[0].reasons)


def test_min_dollar_volume_gate():
    bars = minute_bars("AAA", TODAY, 30, 100.0, 104.0, 10)  # tiny volume
    for d in PRIOR_DAYS:
        bars += minute_bars("AAA", d, 30, 100.0, 100.0, 10)
    results = scan_hot_stocks(["AAA"], client=FakeClient({"AAA": bars}), now=NOW, session_info=verified_session(NOW))
    assert results[0].classification == "filtered"
    assert any("dollar volume" in r for r in results[0].reasons)


def test_eligible_ranks_above_higher_scored_filtered():
    # AAA: strong move but stale data -> filtered with a high score.
    bars_a = []
    for d in PRIOR_DAYS:
        bars_a += minute_bars("AAA", d, 90, 100.0, 100.0, 1_000)
    bars_a += minute_bars("AAA", TODAY, 75, 100.0, 110.0, 10_000)
    # BBB: modest but clean data -> eligible with a lower score.
    bars_b = hot_day_bars("BBB")

    quotes = {
        "AAA": FakeQuote("AAA", NOW, 109.9, 110.1),
        "BBB": FakeQuote("BBB", NOW, 102.9, 103.1),
    }
    results = scan_hot_stocks(
        ["AAA", "BBB"],
        client=FakeClient({"AAA": bars_a, "BBB": bars_b}, quotes),
        now=NOW, session_info=verified_session(NOW),
    )
    by_symbol = {r.symbol: r for r in results}
    assert by_symbol["AAA"].classification == "filtered"
    assert by_symbol["AAA"].score > by_symbol["BBB"].score
    assert results[0].symbol == "BBB"  # eligible always ranks first
