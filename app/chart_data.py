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


def rth_vwap(
    bars: list,
    session_times: dict,
    tz=MARKET_TZ,
) -> list[tuple[datetime, float]]:
    """RTH-anchored VWAP — regular session only.

    `session_times` maps session date -> (open, close) from the official
    exchange calendar (early closes included). Only bars inside each
    verified regular session contribute — premarket volume is never
    mixed in. Zero/missing-volume bars contribute nothing. Days absent
    from the calendar contribute nothing (unverified, not guessed).
    """
    out: list[tuple[datetime, float]] = []
    day = None
    pv = vol_sum = 0.0
    for b in bars:
        ts_et = b.timestamp.astimezone(tz)
        times = session_times.get(ts_et.date())
        if times is None:
            if day != ts_et.date():
                day, pv, vol_sum = ts_et.date(), 0.0, 0.0
            continue  # unverified session -> excluded
        if day != ts_et.date():
            day, pv, vol_sum = ts_et.date(), 0.0, 0.0
        if not (times[0] <= ts_et.time() < times[1]):
            continue  # outside regular hours
        vol = getattr(b, "volume", 0) or 0
        if vol > 0 and b.high and b.low and b.close:
            pv += ((b.high + b.low + b.close) / 3) * vol
            vol_sum += vol
        if vol_sum > 0:
            out.append((b.timestamp, pv / vol_sum))
    return out
