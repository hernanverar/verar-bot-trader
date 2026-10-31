"""Intraday (1-minute) market data from Alpaca. Read-only.

Feed notes: the data feed is controlled by ALPACA_DATA_FEED (default "iex").
The free IEX feed reports only trades executed on the IEX exchange — a subset
of consolidated U.S. volume — and works without a paid subscription. The SIP
feed (full consolidated tape) requires a paid Alpaca data subscription; on
free plans SIP data is additionally delayed ~15 minutes, so near-real-time
intraday scanning is only possible with IEX. All volume figures and relative
volume are computed from whichever feed is configured and inherit its
coverage limitations.
"""

import time
from datetime import date, datetime, time as dtime, timedelta, timezone

from alpaca.common.exceptions import APIError
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from app.market_data import MARKET_TZ, _data_feed, get_data_client

SESSION_OPEN = dtime(9, 30)
SESSION_CLOSE = dtime(16, 0)
DEFAULT_LOOKBACK_DAYS = 15
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0


def _is_retryable(exc: APIError) -> bool:
    status = getattr(exc, "status_code", None)
    return status is None or status == 429 or status >= 500


def fetch_minute_bars(
    client,
    symbols: list[str],
    days: int = DEFAULT_LOOKBACK_DAYS,
    now: datetime | None = None,
) -> dict[str, list]:
    """Return sorted 1-minute bars per symbol for the recent calendar window."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    request = StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=TimeFrame.Minute,
        start=now - timedelta(days=days),
        end=now,
        feed=_data_feed(),
    )
    for attempt in range(MAX_RETRIES):
        try:
            barset = client.get_stock_bars(request)
            break
        except APIError as exc:
            if attempt == MAX_RETRIES - 1 or not _is_retryable(exc):
                raise
            time.sleep(RETRY_BACKOFF_SECONDS * (2 ** attempt))
    data = getattr(barset, "data", barset)
    return {
        symbol: sorted(data.get(symbol, []), key=lambda b: b.timestamp)
        for symbol in symbols
    }


def is_completed_minute_bar(bar_ts: datetime, now: datetime | None = None) -> bool:
    """A 1-minute bar stamped T covers [T, T+1min); it is complete at T+60s."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return bar_ts + timedelta(minutes=1) <= now


def completed_minute_bars(bars: list, now: datetime | None = None) -> list:
    return [b for b in bars if is_completed_minute_bar(b.timestamp, now)]


def bars_by_session(bars: list, tz=MARKET_TZ) -> dict[date, list]:
    """Group bars by their exchange-local session date."""
    sessions: dict[date, list] = {}
    for b in bars:
        d = b.timestamp.astimezone(tz).date()
        sessions.setdefault(d, []).append(b)
    return sessions


def session_cum_volume_at(day_bars: list, cutoff: dtime, tz=MARKET_TZ) -> float:
    """Total volume of one session's bars whose minute starts at/before cutoff."""
    return sum(
        b.volume
        for b in day_bars
        if b.timestamp.astimezone(tz).time() <= cutoff
    )


def filter_regular_session_bars(
    bars: list,
    session_times: dict[date, tuple[dtime, dtime]],
    tz=MARKET_TZ,
) -> list:
    """Keep only bars inside each day's official regular session.

    `session_times` maps session date -> (open, close) from the exchange
    calendar (early closes included). Bars whose session date is absent
    from the map are dropped — unverified sessions are never guessed.

    Used by pullback-pattern prep; NOT applied in fetch_minute_bars so
    other features can still use extended-hours data.
    """
    kept = []
    for b in bars:
        ts_et = b.timestamp.astimezone(tz)
        times = session_times.get(ts_et.date())
        if times is not None and times[0] <= ts_et.time() < times[1]:
            kept.append(b)
    return kept
