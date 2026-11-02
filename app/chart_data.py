"""Chart-series helpers for the Trading Terminal. Read-only, pure functions."""

from datetime import datetime

from app.market_data import MARKET_TZ


def session_vwap(bars: list) -> list[tuple[datetime, float]]:
    """Session-reset VWAP: typical price (H+L+C)/3, volume-weighted.

    Resets at each exchange-local (ET) session boundary so days never mix.
    Covers whatever bars exist in the session — on the IEX feed that is
    effectively the regular session plus sparse extended-hours prints, so
    it is NOT necessarily the consolidated-tape VWAP. Bars with missing
    or zero volume contribute nothing; the last valid value is emitted so
    the series stays continuous without inventing data.
    """
    out: list[tuple[datetime, float]] = []
    day = None
    pv = vol_sum = 0.0
    for b in bars:
        d = b.timestamp.astimezone(MARKET_TZ).date()
        if d != day:
            day, pv, vol_sum = d, 0.0, 0.0
        vol = getattr(b, "volume", 0) or 0
        if vol > 0 and b.high and b.low and b.close:
            pv += ((b.high + b.low + b.close) / 3) * vol
            vol_sum += vol
        if vol_sum > 0:
            out.append((b.timestamp, pv / vol_sum))
    return out
