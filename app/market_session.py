"""U.S. equity market-session awareness. Read-only.

Classifies a moment as pre_market / regular / after_hours / closed using
timezone-aware America/New_York arithmetic. When an Alpaca trading client is
available, the market calendar is consulted so holidays and early closures
are handled correctly; otherwise a weekday 09:30–16:00 ET heuristic applies.

Deterministic and testable: `session_times` and `now` are injectable, and
`trading_days` can be injected instead of a live calendar fetch.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from app.market_data import MARKET_TZ

PRE_MARKET_OPEN = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
AFTER_HOURS_CLOSE = time(20, 0)
CALENDAR_LOOKBACK_DAYS = 10


@dataclass(frozen=True)
class MarketSessionInfo:
    session: str  # pre_market | regular | after_hours | closed
    is_open: bool  # regular session in progress right now
    timezone: str
    as_of: datetime  # aware, ET
    is_trading_day: bool
    session_open: time | None
    session_close: time | None
    last_regular_session: date | None
    calendar_verified: bool  # True when Alpaca calendar supplied the schedule


def _to_et(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(MARKET_TZ)


def classify_session(
    now_et: datetime,
    is_trading_day: bool,
    open_t: time | None = None,
    close_t: time | None = None,
) -> str:
    """Pure session classification for an ET-aware timestamp."""
    if not is_trading_day:
        return "closed"
    t = now_et.time()
    open_t = open_t or REGULAR_OPEN
    close_t = close_t or REGULAR_CLOSE
    if t < PRE_MARKET_OPEN:
        return "closed"
    if t < open_t:
        return "pre_market"
    if t < close_t:
        return "regular"
    if t < AFTER_HOURS_CLOSE:
        return "after_hours"
    return "closed"


def _fetch_trading_days(trading_client, start: date, end: date) -> dict | None:
    """Map of trading date -> (open, close) times; None if the call fails."""
    if trading_client is None:
        return None
    try:
        from alpaca.trading.requests import GetCalendarRequest

        calendar = trading_client.get_calendar(
            GetCalendarRequest(start=start, end=end)
        )
        return {
            entry.date: (
                entry.open if isinstance(entry.open, time) else REGULAR_OPEN,
                entry.close if isinstance(entry.close, time) else REGULAR_CLOSE,
            )
            for entry in calendar
        }
    except Exception:
        return None


def _last_regular_session(
    trading_days: dict, now_et: datetime, open_t: time
) -> date | None:
    """Most recent trading day whose regular session has already opened."""
    today = now_et.date()
    candidates = [
        d for d in trading_days if d < today or (d == today and now_et.time() >= open_t)
    ]
    return max(candidates) if candidates else None


def get_market_session(
    now: datetime | None = None,
    trading_client=None,
    session_times: tuple[time, time] | None | bool = None,
    trading_days: dict | None = None,
) -> MarketSessionInfo:
    """Classify `now` (default: current time) into a market session.

    session_times:
      - None  -> use trading_days/calendar/heuristic for today's schedule
      - False -> force a non-trading day (tests: weekends, holidays)
      - (open, close) -> use these times for today (tests / early closes)

    trading_days: optional {date: (open, close)} map; when omitted and a
    trading_client is given, one bounded calendar fetch covers the window.
    """
    now = now or datetime.now(timezone.utc)
    now_et = _to_et(now)
    day = now_et.date()

    calendar_verified = trading_days is not None
    if trading_days is None and trading_client is not None:
        trading_days = _fetch_trading_days(
            trading_client, day - timedelta(days=CALENDAR_LOOKBACK_DAYS), day
        )
        calendar_verified = trading_days is not None

    if trading_days is None:
        trading_days = {
            day - timedelta(days=i): (REGULAR_OPEN, REGULAR_CLOSE)
            for i in range(CALENDAR_LOOKBACK_DAYS + 1)
            if (day - timedelta(days=i)).weekday() < 5
        }

    if session_times is None:
        today_times = trading_days.get(day)
        session_times = today_times if today_times is not None else False
    if session_times is not False and day not in trading_days:
        # Injected explicit times take precedence for today.
        trading_days = {**trading_days, day: session_times}

    is_trading_day = session_times is not False
    open_t, close_t = session_times if is_trading_day else (None, None)
    session = classify_session(now_et, is_trading_day, open_t, close_t)

    return MarketSessionInfo(
        session=session,
        is_open=session == "regular",
        timezone="America/New_York",
        as_of=now_et,
        is_trading_day=is_trading_day,
        session_open=open_t,
        session_close=close_t,
        last_regular_session=_last_regular_session(
            trading_days, now_et, open_t or REGULAR_OPEN
        ),
        calendar_verified=calendar_verified,
    )
