from datetime import date, datetime, time, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.market_session import classify_session, get_market_session
from app.market_discovery import classify_instrument

ET = ZoneInfo("America/New_York")

# Thursday 2026-01-15
def et(hour, minute=0, day=15):
    return datetime(2026, 1, day, hour, minute, tzinfo=ET)


def test_regular_session():
    info = get_market_session(now=et(11, 0))
    assert info.session == "regular" and info.is_open
    assert info.session_open == time(9, 30) and info.session_close == time(16, 0)
    assert info.last_regular_session == date(2026, 1, 15)


def test_premarket_session():
    info = get_market_session(now=et(7, 0))
    assert info.session == "pre_market" and not info.is_open
    # Today's regular session hasn't opened yet -> last is prior weekday.
    assert info.last_regular_session == date(2026, 1, 14)


def test_after_hours_session():
    info = get_market_session(now=et(18, 0))
    assert info.session == "after_hours" and not info.is_open
    assert info.last_regular_session == date(2026, 1, 15)


def test_weekend_is_closed():
    info = get_market_session(now=et(10, 0, day=17))  # Saturday
    assert info.session == "closed" and not info.is_open
    assert info.last_regular_session == date(2026, 1, 16)  # Friday


def test_holiday_via_session_times_false():
    info = get_market_session(now=et(11, 0), session_times=False)
    assert info.session == "closed" and not info.is_trading_day


def test_early_close():
    info = get_market_session(now=et(13, 30), session_times=(time(9, 30), time(13, 0)))
    assert info.session == "after_hours"
    assert info.session_close == time(13, 0)


def test_classify_pure():
    assert classify_session(et(10), True) == "regular"
    assert classify_session(et(5), True) == "pre_market"
    assert classify_session(et(17), True) == "after_hours"
    assert classify_session(et(22), True) == "closed"
    assert classify_session(et(10), False) == "closed"


def test_calendar_client_used_for_today():
    day = date(2026, 1, 15)
    client = SimpleNamespace(
        get_calendar=lambda req: [
            SimpleNamespace(date=day, open=time(9, 30), close=time(13, 0)),
            SimpleNamespace(date=day.replace(day=14), open=time(9, 30), close=time(16, 0)),
        ]
    )
    info = get_market_session(now=et(14, 0), trading_client=client)
    assert info.calendar_verified and info.session == "after_hours"


def test_instrument_classification():
    common = SimpleNamespace(asset_class="us_equity", tradable=True, name="Apple Inc.")
    adr = SimpleNamespace(asset_class="us_equity", tradable=True,
                          name="Toyota Motor Corp. American Depositary Shares")
    warrant_asset = SimpleNamespace(asset_class="us_equity", tradable=True, name="Foo Warrant")
    preferred_asset = SimpleNamespace(asset_class="us_equity", tradable=True,
                                      name="Bar 7% Preferred Stock")
    option = SimpleNamespace(asset_class="us_option", tradable=True, name="X")
    untradable = SimpleNamespace(asset_class="us_equity", tradable=False, name="Halt Corp.")

    assert classify_instrument("AAPL", common) == "common_stock"
    assert classify_instrument("TM", adr) == "adr"
    assert classify_instrument("XYZ.WS", warrant_asset) == "warrant"
    assert classify_instrument("ABC.RT", None) == "right"  # suffix safeguard
    assert classify_instrument("DEF.UN", None) == "unit"
    assert classify_instrument("GHI.PRA", None) == "preferred"
    assert classify_instrument("FOO", option) == "unsupported"
    assert classify_instrument("HALT", untradable) == "unsupported"
    assert classify_instrument("UNITI", SimpleNamespace(asset_class="us_equity",
                            tradable=True, name="Uniti Group Inc.")) == "common_stock"
    # Legit dotted class shares are preserved.
    assert classify_instrument("BRK.B", common) == "common_stock"
    # No metadata -> unverified, never silently claimed common.
    assert classify_instrument("MYSTERY", None) == "unverified"


def test_filtering_applies_before_cap():
    from app.market_discovery import discover_momentum_universe, _ASSET_CACHE
    _ASSET_CACHE.clear()

    screener = SimpleNamespace(
        get_market_movers=lambda req: SimpleNamespace(
            gainers=[SimpleNamespace(symbol=s, percent_change=5, change=1, price=10)
                     for s in ["AAA", "BAD.WS", "BBB", "ZZZ.RT", "CCC"]],
            losers=[], last_updated=None),
        get_most_actives=lambda req: SimpleNamespace(most_actives=[], last_updated=None),
    )

    class FakeTrading:
        def get_asset(self, symbol):
            return SimpleNamespace(asset_class="us_equity", tradable=True, name="Common Inc.")

    result = discover_momentum_universe(
        screener_client=screener, trading_client=FakeTrading(),
        max_symbols=3, now=datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc),
    )
    assert result.symbols == ("AAA", "BBB", "CCC")  # cap fills after exclusions
    assert result.excluded_instruments_count == 2


def test_no_live_eligible_outside_regular_session():
    """Scanner integration: identical data, regular vs after-hours."""
    from test_hot_stock_scanner import (
        FakeClient, FakeQuote, NOW, PRIOR_DAYS, TODAY, minute_bars,
        verified_session,
    )
    from app.strategies.hot_stock_scanner import scan_hot_stocks

    # Full regular session of bars (9:30 -> 15:59), gapped up ~4% on 3x volume.
    bars = []
    for d in PRIOR_DAYS:
        bars += minute_bars("AAA", d, 90, 100.0, 100.0, 1_000)
    bars += minute_bars("AAA", TODAY, 390, 104.0, 104.5, 3_000)
    quotes = {"AAA": FakeQuote("AAA", NOW, 102.9, 103.1)}

    # Same run at 11:00 ET (regular): eligible.
    regular = scan_hot_stocks(
        ["AAA"], client=FakeClient({"AAA": bars}, quotes),
        now=NOW, session_info=verified_session(NOW),
    )
    assert regular[0].classification == "eligible"

    # At 18:00 ET (after-hours): same data -> research, never eligible.
    evening = NOW.replace(hour=18)
    after = scan_hot_stocks(
        ["AAA"], client=FakeClient({"AAA": bars}, quotes),
        now=evening, session_info=verified_session(evening),
    )
    assert after[0].classification == "research"
    assert after[0].research_eligible is True
    assert after[0].bar_status == "session_ended"


def test_session_ended_vs_unexpectedly_stale_bars():
    from test_hot_stock_scanner import (
        FakeClient, FakeQuote, NOW, PRIOR_DAYS, TODAY, minute_bars,
        verified_session,
    )
    from app.strategies.hot_stock_scanner import scan_hot_stocks

    evening = NOW.replace(hour=18)
    # AAA: final bar at 15:59 ET today -> session_ended (expected).
    normal = []
    for d in PRIOR_DAYS:
        normal += minute_bars("AAA", d, 90, 100.0, 100.0, 1_000)
    normal += minute_bars("AAA", TODAY, 389, 100.0, 104.0, 3_000)  # ends 15:58..15:59
    # BBB: bars stop at 11:00 today -> stale mid-session gap.
    short = []
    for d in PRIOR_DAYS:
        short += minute_bars("BBB", d, 90, 100.0, 100.0, 1_000)
    short += minute_bars("BBB", TODAY, 90, 100.0, 104.0, 3_000)

    quotes = {
        "AAA": FakeQuote("AAA", evening, 103.9, 104.1),
        "BBB": FakeQuote("BBB", evening, 103.9, 104.1),
    }
    results = scan_hot_stocks(
        ["AAA", "BBB"], client=FakeClient({"AAA": normal, "BBB": short}, quotes), now=evening, session_info=verified_session(evening),
    )
    by_symbol = {r.symbol: r for r in results}
    assert by_symbol["AAA"].bar_status == "session_ended"
    assert by_symbol["AAA"].classification == "research"
    assert by_symbol["BBB"].bar_status == "stale"
    assert by_symbol["BBB"].classification == "filtered"
    assert any("bars end before latest session close" in r for r in by_symbol["BBB"].reasons)


def test_scanner_preserves_stale_gate_during_regular():
    from test_hot_stock_scanner import FakeClient, NOW, minute_bars, verified_session
    from app.strategies.hot_stock_scanner import scan_hot_stocks

    bars = minute_bars("AAA", NOW.date(), 30, 100.0, 104.0, 5_000)
    results = scan_hot_stocks(["AAA"], client=FakeClient({"AAA": bars}),
                                 now=NOW, session_info=verified_session(NOW))
    assert results[0].bar_status == "stale"
    assert results[0].classification == "filtered"
    assert any(r.startswith("stale bars") for r in results[0].reasons)


def test_leveraged_inverse_etf_excluded():
    """MSTZ-style inverse leveraged ETF must not pass as common stock."""
    mstz = SimpleNamespace(
        asset_class="us_equity", tradable=True,
        name="T-Rex 2X Inverse MSTR Daily Target ETF",
    )
    assert classify_instrument("MSTZ", mstz) == "etf"
    for etf_name in [
        "SPDR S&P 500 ETF Trust",
        "ProShares UltraPro QQQ",
        "iPath Series B S&P 500 VIX ETN",
        "Direxion Daily Semiconductor Bull 3X Shares",
    ]:
        asset = SimpleNamespace(asset_class="us_equity", tradable=True, name=etf_name)
        assert classify_instrument("XXX", asset) == "etf", etf_name


def test_filter_excludes_etf_keeps_common_and_adr():
    from app.market_discovery import discover_momentum_universe, _ASSET_CACHE
    _ASSET_CACHE.clear()

    screener = SimpleNamespace(
        get_market_movers=lambda req: SimpleNamespace(
            gainers=[SimpleNamespace(symbol=s, percent_change=5, change=1, price=10)
                     for s in ["MSTZ", "AAPL", "TM"]],
            losers=[], last_updated=None),
        get_most_actives=lambda req: SimpleNamespace(most_actives=[], last_updated=None),
    )
    names = {
        "MSTZ": "T-Rex 2X Inverse MSTR Daily Target ETF",
        "AAPL": "Apple Inc.",
        "TM": "Toyota Motor Corp. American Depositary Shares",
    }

    class FakeTrading:
        def get_asset(self, symbol):
            return SimpleNamespace(asset_class="us_equity", tradable=True, name=names[symbol])

    result = discover_momentum_universe(
        screener_client=screener, trading_client=FakeTrading(),
        now=datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc),
    )
    assert "MSTZ" not in result.symbols
    assert set(result.symbols) == {"AAPL", "TM"}
    assert result.excluded_instruments_count == 1


def test_unverified_session_blocks_eligibility_not_research():
    """Heuristic weekday regular session: no live eligibility, research allowed."""
    from test_hot_stock_scanner import (
        FakeClient, FakeQuote, NOW, PRIOR_DAYS, TODAY, minute_bars,
        verified_session,
    )
    from app.market_session import MarketSessionInfo
    from app.strategies.hot_stock_scanner import scan_hot_stocks

    bars = []
    for d in PRIOR_DAYS:
        bars += minute_bars("AAA", d, 90, 100.0, 100.0, 1_000)
    bars += minute_bars("AAA", TODAY, 90, 104.0, 104.5, 3_000)
    quotes = {"AAA": FakeQuote("AAA", NOW, 104.4, 104.6)}

    verified = verified_session(NOW)
    unverified = MarketSessionInfo(
        session="regular", is_open=True, timezone="America/New_York",
        as_of=verified.as_of, is_trading_day=True,
        session_open=verified.session_open, session_close=verified.session_close,
        last_regular_session=verified.last_regular_session,
        calendar_verified=False,
    )

    ok = scan_hot_stocks(["AAA"], client=FakeClient({"AAA": bars}, quotes),
                         now=NOW, session_info=verified)
    assert ok[0].classification == "eligible"

    blocked = scan_hot_stocks(["AAA"], client=FakeClient({"AAA": bars}, quotes),
                              now=NOW, session_info=unverified)
    assert blocked[0].classification == "research"
    assert blocked[0].research_eligible is True


def test_holiday_via_injected_trading_days():
    """Injected calendar without today -> closed even on a weekday."""
    from app.market_session import get_market_session
    from test_hot_stock_scanner import PRIOR_DAYS
    from datetime import time as t

    days = {d: (t(9, 30), t(16, 0)) for d in PRIOR_DAYS}  # today absent -> holiday
    info = get_market_session(now=datetime(2026, 1, 15, 11, 0, tzinfo=ET),
                              trading_days=days)
    assert info.is_trading_day is False
    assert info.session == "closed"
    assert info.calendar_verified is True
