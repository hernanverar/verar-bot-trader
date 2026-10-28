from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import market_data as md
from app.scanner import StockMetrics

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 1, 15, 10, 0, tzinfo=ET)  # Thursday, mid-session


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
    """Stands in for StockHistoricalDataClient; records requests, returns fixtures."""

    def __init__(self, bars_by_symbol, quotes=None):
        self.bars_by_symbol = bars_by_symbol
        self.quotes = quotes or {}
        self.bars_request = None
        self.quote_request = None

    def get_stock_bars(self, request_params):
        self.bars_request = request_params
        return FakeBarSet(self.bars_by_symbol)

    def get_stock_latest_quote(self, request_params):
        self.quote_request = request_params
        return self.quotes


def make_bars(symbol, closes, volumes=None, end_date=date(2026, 1, 15)):
    volumes = volumes or [1_000_000] * len(closes)
    bars = []
    for i, (close, volume) in enumerate(zip(closes, volumes)):
        d = end_date - timedelta(days=len(closes) - 1 - i)
        ts = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
        bars.append(FakeBar(symbol, ts, close, close * 1.01, close * 0.99, close, volume))
    return bars


def make_quote(symbol, bid, ask):
    return FakeQuote(symbol, NOW, bid, ask)


def test_completed_filter_drops_todays_bar_during_session():
    bars = make_bars("X", [100.0] * 25)
    completed = md.completed_daily_bars(bars, now=NOW)
    assert len(completed) == 24
    assert completed[-1].timestamp.date() == date(2026, 1, 14)


def test_completed_filter_keeps_todays_bar_after_close():
    bars = make_bars("X", [100.0] * 25)
    after_close = NOW.replace(hour=17)
    assert len(md.completed_daily_bars(bars, now=after_close)) == 25


def test_spread_bps_from_quote():
    assert md.spread_bps_from_quote(make_quote("X", 99.9, 100.1)) == pytest.approx(20.0)
    assert md.spread_bps_from_quote(None) is None
    assert md.spread_bps_from_quote(make_quote("X", 0.0, 0.0)) is None


def test_metrics_from_bars_computation():
    closes = [100.0 + i for i in range(21)]
    volumes = [1_000_000] * 20 + [3_000_000]
    metrics = md.metrics_from_bars("X", make_bars("X", closes, volumes), spread_bps=10.0)
    assert metrics == StockMetrics("X", pytest.approx(0.20), pytest.approx(3.0), 122_500_000, 10.0)


def test_metrics_from_bars_requires_enough_bars_and_spread():
    bars = make_bars("X", [100.0] * 20)
    assert md.metrics_from_bars("X", bars, spread_bps=10.0) is None
    enough = make_bars("X", [100.0] * 21)
    assert md.metrics_from_bars("X", enough, spread_bps=None) is None


def test_scan_symbols_ranks_and_reports_reasons():
    closes = [100.0 + i for i in range(25)]
    bars_by_symbol = {
        "AAA": make_bars("AAA", closes),
        "BBB": make_bars("BBB", [100.0] * 10),
        "CCC": make_bars("CCC", closes),
        "DDD": [],
        "EEE": make_bars("EEE", closes),
    }
    quotes = {
        "AAA": make_quote("AAA", 119.95, 120.05),
        "CCC": None,
        "EEE": make_quote("EEE", 119.0, 121.0),  # ~168 bps spread -> filtered out
    }
    results = md.scan_symbols(["AAA", "BBB", "CCC", "DDD", "EEE"], client=FakeClient(bars_by_symbol, quotes), now=NOW)

    by_ticker = {r.ticker: r for r in results}
    assert by_ticker["AAA"].score is not None and by_ticker["AAA"].reason is None
    assert by_ticker["BBB"].reason == "insufficient bars (9/21)"
    assert by_ticker["CCC"].reason == "no usable quote"
    assert by_ticker["DDD"].reason == "no market data"
    assert by_ticker["EEE"].reason == "filtered by liquidity/spread thresholds"
    assert results[0].ticker == "AAA"
    assert by_ticker["AAA"].as_of.date() == date(2026, 1, 14)  # today's partial bar excluded
    assert by_ticker["DDD"].as_of is None


def test_scan_symbols_uses_daily_bars_request():
    client = FakeClient({"AAA": make_bars("AAA", [100.0] * 25)}, {"AAA": make_quote("AAA", 99.9, 100.1)})
    md.scan_symbols(["AAA"], client=client, now=NOW)
    req = client.bars_request
    assert req.symbol_or_symbols == ["AAA"]
    assert str(req.timeframe) == "1Day" or req.timeframe.amount == 1
    assert client.quote_request.symbol_or_symbols == ["AAA"]


def test_format_scan_results():
    results = md.scan_symbols(
        ["AAA", "DDD"],
        client=FakeClient({"AAA": make_bars("AAA", [100.0 + i for i in range(25)])}, {"AAA": make_quote("AAA", 99.9, 100.1)}),
        now=NOW,
    )
    text = md.format_scan_results(results)
    assert "AAA" in text and "data as of: 2026-01-14" in text
    assert "DDD" in text and "no market data" in text
