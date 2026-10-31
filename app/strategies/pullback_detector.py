"""Deterministic intraday pullback-pattern detector. Research-only.

Scans completed 1-minute OHLCV bars for a three-stage sequence:

1. Bullish impulse — a run from a local low up to a subsequent high.
2. Controlled pullback — a bounded retracement of that impulse.
3. Recovery — a close above the previous completed bar's high.

No lookahead bias: each pattern is resolved using only bars at or before
the bar that triggers the state change. Parameters are experimental
research defaults, not validated trading rules.

Gap policy: a timestamp gap larger than `max_gap_minutes`, or a change of
exchange-local session date, splits the analysis into independent
segments — patterns are never stitched across missing data or sessions.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.market_data import MARKET_TZ

IMPULSE_DETECTED = "impulse_detected"
PULLBACK_IN_PROGRESS = "pullback_in_progress"
RECOVERY_CONFIRMED = "recovery_confirmed"
INVALIDATED = "invalidated"
_PATTERN_STATES = {
    IMPULSE_DETECTED, PULLBACK_IN_PROGRESS, RECOVERY_CONFIRMED, INVALIDATED,
}


@dataclass(frozen=True)
class PullbackConfig:
    min_impulse_pct: float = 5.0      # percent gain from impulse low to high
    impulse_min_bars: int = 5         # min bars from impulse low to high
    impulse_max_bars: int = 30        # max bars from impulse low to high
    retrace_min: float = 0.20         # fraction of impulse range
    retrace_max: float = 0.60
    pullback_min_bars: int = 2        # min bars after impulse high
    pullback_max_bars: int = 15
    max_gap_minutes: int = 3          # larger gaps split the analysis


@dataclass(frozen=True)
class PullbackPattern:
    symbol: str
    state: str
    impulse_start_ts: datetime
    impulse_high_ts: datetime
    impulse_low: float
    impulse_high: float
    impulse_gain_pct: float
    pullback_low: float | None
    pullback_low_ts: datetime | None
    retracement_pct: float | None
    pullback_bars: int | None
    recovery_ts: datetime | None
    invalidation_reason: str | None


def _validate_bars(bars: list) -> None:
    """Reject malformed input — never silently sort or fabricate."""
    prev_ts = None
    for b in bars:
        if prev_ts is not None and b.timestamp <= prev_ts:
            raise ValueError("bars must be strictly chronological with unique timestamps")
        prev_ts = b.timestamp
        for field in ("open", "high", "low", "close"):
            if getattr(b, field) is None or getattr(b, field) <= 0:
                raise ValueError(f"non-positive OHLC price on {b.timestamp}")
        if b.high < b.low or b.high < b.open or b.high < b.close:
            raise ValueError(f"inconsistent OHLC on {b.timestamp}")


def _segments(bars: list, max_gap: timedelta):
    """Split on session-date changes and timestamp gaps — no stitching."""
    segment = [bars[0]]
    for prev, b in zip(bars, bars[1:]):
        same_day = (
            b.timestamp.astimezone(MARKET_TZ).date()
            == prev.timestamp.astimezone(MARKET_TZ).date()
        )
        if not same_day or b.timestamp - prev.timestamp > max_gap:
            yield segment
            segment = []
        segment.append(b)
    yield segment


def _find_impulse(seg: list, start: int, config: PullbackConfig):
    """First qualifying impulse at/after `start`, or None.

    The impulse low is the segment's running minimum low; a newer lower
    low simply re-anchors the candidate (no future data involved).
    """
    n = len(seg)
    for j in range(start, n):
        low = seg[j].low
        if low <= 0:
            continue
        max_high, hi_idx = low, j
        for k in range(j + 1, min(n, j + config.impulse_max_bars)):
            if seg[k].low < low:
                break  # j is not the impulse low anymore
            if seg[k].high > max_high:
                max_high, hi_idx = seg[k].high, k
            if (
                hi_idx - j + 1 >= config.impulse_min_bars
                and max_high / low - 1 >= config.min_impulse_pct / 100
            ):
                return j, hi_idx, low, max_high
    return None


def _track_pullback(
    seg: list,
    symbol: str,
    low_idx: int,
    hi_idx: int,
    impulse_low: float,
    impulse_high: float,
    config: PullbackConfig,
) -> tuple[PullbackPattern, int]:
    """Walk bars after the impulse high; return (pattern, resume_index)."""
    impulse_range = impulse_high - impulse_low
    pb_low = float("inf")
    pb_low_idx = -1

    def finish(state, reason, recovery_ts=None, idx=None):
        retr = (
            round((impulse_high - pb_low) / impulse_range * 100, 2)
            if pb_low_idx >= 0 and impulse_range > 0
            else None
        )
        return PullbackPattern(
            symbol=symbol,
            state=state,
            impulse_start_ts=seg[low_idx].timestamp,
            impulse_high_ts=seg[hi_idx].timestamp,
            impulse_low=impulse_low,
            impulse_high=impulse_high,
            impulse_gain_pct=round((impulse_high / impulse_low - 1) * 100, 2),
            pullback_low=pb_low if pb_low_idx >= 0 else None,
            pullback_low_ts=seg[pb_low_idx].timestamp if pb_low_idx >= 0 else None,
            retracement_pct=retr,
            pullback_bars=(idx - hi_idx) if idx is not None else (pb_low_idx - hi_idx if pb_low_idx >= 0 else None),
            recovery_ts=recovery_ts,
            invalidation_reason=reason,
        ), (idx if idx is not None else len(seg))

    for k in range(hi_idx + 1, len(seg)):
        b = seg[k]
        duration = k - hi_idx

        if duration > config.pullback_max_bars:
            return finish(INVALIDATED, "pullback exceeded max duration", idx=k - 1)

        if duration < config.pullback_min_bars:
            # A new high before the pullback establishes extends the
            # impulse instead — re-anchor deterministically.
            if b.high > impulse_high:
                impulse_high, hi_idx = b.high, k
                impulse_range = impulse_high - impulse_low
                pb_low, pb_low_idx = float("inf"), -1
                continue
            if b.low < impulse_low:
                return finish(INVALIDATED, "broke below impulse low", idx=k)
            if b.low < pb_low:
                pb_low, pb_low_idx = b.low, k
            if impulse_range > 0 and (impulse_high - pb_low) / impulse_range > config.retrace_max:
                return finish(INVALIDATED, "retracement exceeded max", idx=k)
            continue

        # Established-pullback phase: a breakout is judged by the close.
        if b.low < impulse_low:
            return finish(INVALIDATED, "broke below impulse low", idx=k)
        if b.low < pb_low:
            pb_low, pb_low_idx = b.low, k

        if impulse_range > 0:
            retr = (impulse_high - pb_low) / impulse_range
            if retr > config.retrace_max:
                return finish(INVALIDATED, "retracement exceeded max", idx=k)
            if b.close > seg[k - 1].high:
                if retr < config.retrace_min:
                    return finish(INVALIDATED, "pullback too shallow before recovery", idx=k)
                return finish(RECOVERY_CONFIRMED, None, recovery_ts=b.timestamp, idx=k)

    return finish(PULLBACK_IN_PROGRESS, None)


def detect_pullbacks(
    bars: list,
    config: PullbackConfig | None = None,
    symbol: str | None = None,
) -> list[PullbackPattern]:
    """Detect pullback patterns in completed chronological 1-minute bars."""
    config = config or PullbackConfig()
    _validate_bars(bars)
    patterns: list[PullbackPattern] = []
    if not bars:
        return patterns

    for seg in _segments(bars, timedelta(minutes=config.max_gap_minutes)):
        i = 0
        while True:
            found = _find_impulse(seg, i, config)
            if found is None:
                break
            low_idx, hi_idx, low, high = found
            sym = symbol or getattr(seg[low_idx], "symbol", "")
            pattern, resume = _track_pullback(
                seg, sym, low_idx, hi_idx, low, high, config
            )
            patterns.append(pattern)
            i = resume + 1
            if i >= len(seg):
                break

    return patterns
