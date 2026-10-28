"""Fetch daily market data from Alpaca and convert it to scanner metrics.

Read-only: this module only calls market-data endpoints (bars, quotes).
It never submits orders and never touches the trading endpoints.
"""

import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from dotenv import load_dotenv

from app.scanner import StockMetrics, score_stock

load_dotenv()

MARKET_TZ = ZoneInfo("America/New_York")
MARKET_CLOSE = time(16, 0)

DEFAULT_SYMBOLS = ["NVDA", "TSLA", "GME", "AMC", "AAPL", "MSFT"]
# Buffer over the 20-session metric window to absorb weekends/holidays.
DEFAULT_LOOKBACK_DAYS = 45
MOMENTUM_WINDOW = 20
REQUIRED_BARS = MOMENTUM_WINDOW + 1


@dataclass(frozen=True)
class ScanResult:
    ticker: str
    score: float | None
    reason: str | None
    as_of: datetime | None


def get_data_client() -> StockHistoricalDataClient:
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY environment variables")
    return StockHistoricalDataClient(api_key, secret_key)


def _data_feed() -> DataFeed:
    return DataFeed(os.getenv("ALPACA_DATA_FEED", "iex").lower())


def fetch_daily_bars(
    client: StockHistoricalDataClient,
    symbols: list[str],
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    now: datetime | None = None,
) -> dict[str, list]:
    """Return raw daily bars per symbol (may include an incomplete bar for today)."""
    now = now or datetime.now(timezone.utc)
    request = StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=TimeFrame.Day,
        start=now - timedelta(days=lookback_days),
        end=now,
        adjustment=Adjustment.ALL,
        feed=_data_feed(),
    )
    barset = client.get_stock_bars(request)
    data = getattr(barset, "data", barset)
    return {symbol: list(data.get(symbol, [])) for symbol in symbols}


def is_completed_daily_bar(bar_ts: datetime, now: datetime | None = None) -> bool:
    """A daily bar is complete once the regular session for its date has closed."""
    now = now or datetime.now(MARKET_TZ)
    now_et = now.astimezone(MARKET_TZ)
    bar_date = bar_ts.astimezone(MARKET_TZ).date()
    if bar_date < now_et.date():
        return True
    if bar_date == now_et.date():
        return now_et.time() >= MARKET_CLOSE
    return False


def completed_daily_bars(bars: list, now: datetime | None = None) -> list:
    return [b for b in bars if is_completed_daily_bar(b.timestamp, now)]


def fetch_latest_quotes(client: StockHistoricalDataClient, symbols: list[str]) -> dict:
    request = StockLatestQuoteRequest(symbol_or_symbols=list(symbols), feed=_data_feed())
    return client.get_stock_latest_quote(request)


def spread_bps_from_quote(quote) -> float | None:
    """Quoted spread in basis points of the mid price, or None if unusable."""
    if quote is None or not quote.ask_price or not quote.bid_price:
        return None
    mid = (quote.ask_price + quote.bid_price) / 2
    if mid <= 0:
        return None
    return (quote.ask_price - quote.bid_price) / mid * 10_000


def metrics_from_bars(ticker: str, bars: list, spread_bps: float | None) -> StockMetrics | None:
    """Build StockMetrics from completed daily bars sorted ascending.

    Needs at least REQUIRED_BARS bars and a usable spread; returns None otherwise.
    """
    if len(bars) < REQUIRED_BARS or spread_bps is None:
        return None
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    if closes[-1] <= 0 or closes[-REQUIRED_BARS] <= 0:
        return None
    prior_volumes = volumes[-REQUIRED_BARS:-1]
    avg_prior_volume = sum(prior_volumes) / len(prior_volumes)

    momentum_20d = closes[-1] / closes[-REQUIRED_BARS] - 1
    relative_volume = volumes[-1] / avg_prior_volume if avg_prior_volume > 0 else 0.0
    recent = bars[-MOMENTUM_WINDOW:]
    avg_dollar_volume = sum(b.close * b.volume for b in recent) / len(recent)
    return StockMetrics(ticker, momentum_20d, relative_volume, avg_dollar_volume, spread_bps)


def scan_symbols(
    symbols: list[str],
    client: StockHistoricalDataClient | None = None,
    now: datetime | None = None,
) -> list[ScanResult]:
    """Fetch market data and rank symbols. Scored results sort first, descending."""
    client = client or get_data_client()
    bars_by_symbol = fetch_daily_bars(client, symbols, now=now)
    quotes = fetch_latest_quotes(client, symbols)

    results = []
    for symbol in symbols:
        bars = sorted(completed_daily_bars(bars_by_symbol.get(symbol, []), now), key=lambda b: b.timestamp)
        as_of = bars[-1].timestamp if bars else None
        spread = spread_bps_from_quote(quotes.get(symbol))

        score, reason = None, None
        if not bars:
            reason = "no market data"
        elif len(bars) < REQUIRED_BARS:
            reason = f"insufficient bars ({len(bars)}/{REQUIRED_BARS})"
        elif spread is None:
            reason = "no usable quote"
        else:
            score = score_stock(metrics_from_bars(symbol, bars, spread))
            if score is None:
                reason = "filtered by liquidity/spread thresholds"
        results.append(ScanResult(symbol, score, reason, as_of))

    return sorted(results, key=lambda r: (r.score is None, -(r.score or 0), r.ticker))


def format_scan_results(results: list[ScanResult]) -> str:
    lines = []
    for r in results:
        as_of = r.as_of.astimezone(MARKET_TZ).strftime("%Y-%m-%d %H:%M %Z") if r.as_of else "n/a"
        score = f"{r.score:.1f}" if r.score is not None else f"n/a ({r.reason})"
        lines.append(f"{r.ticker:<6} score: {score:<45} data as of: {as_of}")
    return "\n".join(lines)
