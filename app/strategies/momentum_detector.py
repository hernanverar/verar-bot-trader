"""Ross-style 1-minute momentum setup detector — pure, deterministic, testable.

Consumes COMPLETED regular-session 1-minute bars only (callers must
pre-filter). Every decision uses only bars at or before the current
timestamp — no lookahead.

State machine: IDLE -> IMPULSE -> PULLBACK -> CONFIRMED | INVALIDATED
then resets to IDLE for the next non-overlapping candidate.

Definitions (frozen, documented):
- Impulse anchor: lowest `low` inside the trailing `impulse_window_bars`
  window at the bar where the impulse first qualifies.
- Impulse: qualifies when `high >= anchor * (1 + impulse_min_pct/100)`.
  `impulse_high` extends while bars keep making new highs; the high is
  frozen once the pullback begins.
- Pullback: bars after impulse where `high <= impulse_high` and `close`
  declines. depth = (impulse_high - bar.low) / (impulse_high - anchor);
  structural depth is measured against `pullback_low` (min low seen).
- Breakout confirmation: first bar after >= pullback_min_bars pullback
  bars with `close > impulse_high` AND `volume >=
  breakout_conf_vol_x * avg(pullback volumes)`.
- Invalidation: pullback low breaches `anchor` side beyond
  `pullback_max_retrace` of impulse range, pullback exceeds
  `pullback_max_bars`, a gap > `max_gap_minutes` or session boundary, or
  a low-volume breakout (weak confirmation = rejected setup).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class _State(Enum):
    IDLE = "idle"
    IMPULSE = "impulse"
    PULLBACK = "pullback"


@dataclass(frozen=True)
class MomentumSetupConfig:
    impulse_min_pct: float = 5.0      # percent
    impulse_window_bars: int = 8
    pullback_max_retrace: float = 0.50  # fraction of impulse range
    pullback_min_bars: int = 1
    pullback_max_bars: int = 8
    breakout_conf_vol_x: float = 1.5
    target_r_multiple: float = 2.0
    max_gap_minutes: int = 3


@dataclass(frozen=True)
class MomentumSetup:
    symbol: str | None
    impulse_anchor_ts: datetime
    impulse_high: float
    anchor_low: float
    pullback_low: float
    pullback_bars: int
    confirm_ts: datetime
    confirm_price: float          # close of breakout bar
    confirm_volume: float
    stop_price: float             # structural: pullback low
    pending_entry: bool           # True — entry resolved by consumer at next eligible bar


@dataclass(frozen=True)
class RejectedSetup:
    ts: datetime
    reason: str


def _gap(bars, i, max_minutes) -> bool:
    return (
        (bars[i].timestamp - bars[i - 1].timestamp).total_seconds()
        > max_minutes * 60
    )


def detect_momentum_setups(
    bars: list,
    config: MomentumSetupConfig | None = None,
    symbol: str | None = None,
) -> tuple[list[MomentumSetup], list[RejectedSetup]]:
    """Chronological detection over completed RTH 1m bars.

    Returns (confirmed setups, rejections) in deterministic order.
    Confirmed setups carry `pending_entry` — no trade is implied; the
    consumer resolves the entry at the next eligible completed bar.
    """
    cfg = config or MomentumSetupConfig()
    setups: list[MomentumSetup] = []
    rejected: list[RejectedSetup] = []
    if not bars:
        return setups, rejected

    state = _State.IDLE
    idle_start = 0                       # anchors only after last reset —
    anchor = impulse_high = pullback_low = 0.0  # never reuse a prior impulse
    anchor_ts = bars[0].timestamp
    impulse_vols: list[float] = []
    pullback_vols: list[float] = []
    pullback_count = 0

    def reset() -> None:
        nonlocal state, impulse_vols, pullback_vols, pullback_count
        state = _State.IDLE
        impulse_vols, pullback_vols, pullback_count = [], [], 0

    for i, b in enumerate(bars):
        ts = b.timestamp

        if i > 0 and _gap(bars, i, cfg.max_gap_minutes):
            if state != _State.IDLE:
                rejected.append(RejectedSetup(ts, "gap_or_session_boundary"))
            reset()

        if state == _State.IDLE:
            lo = min(x.low for x in bars[max(idle_start, i - cfg.impulse_window_bars + 1): i + 1])
            if lo > 0 and b.high >= lo * (1 + cfg.impulse_min_pct / 100):
                state = _State.IMPULSE
                anchor, anchor_ts = lo, ts
                impulse_high = b.high
                impulse_vols = [b.volume or 0.0]
            continue

        if state == _State.IMPULSE:
            if b.high > impulse_high:
                impulse_high = b.high          # still running
                impulse_vols.append(b.volume or 0.0)
            elif b.close < impulse_high and b.close < bars[i - 1].close:
                # Pullback requires real contraction: declining close, not
                # merely "not a new high" (sideways/equal bars stay IMPULSE).
                # First pullback bar is depth-checked like any other —
                # an immediate deep break invalidates, not "pullbacks".
                rng0 = impulse_high - anchor
                d0 = (impulse_high - b.low) / rng0 if rng0 > 0 else 1.0
                if d0 > cfg.pullback_max_retrace or b.low <= anchor:
                    rejected.append(RejectedSetup(ts, "breakdown_below_anchor"))
                    reset()
                    continue
                state = _State.PULLBACK        # first pullback bar
                pullback_low = b.low
                pullback_vols = [b.volume or 0.0]
                pullback_count = 1
            continue

        # PULLBACK
        rng = impulse_high - anchor
        depth = (impulse_high - b.low) / rng if rng > 0 else 1.0
        if depth > cfg.pullback_max_retrace or b.low <= anchor:
            rejected.append(RejectedSetup(ts, "breakdown_below_anchor"))
            reset()
            idle_start = i + 1
            continue
        if b.close > impulse_high:
            # Breakout bar: requires pullback structure + volume confirm.
            if pullback_count < cfg.pullback_min_bars:
                rejected.append(RejectedSetup(ts, "insufficient_pullback"))
            else:
                pb_avg = sum(pullback_vols) / len(pullback_vols) if pullback_vols else 0.0
                if (b.volume or 0.0) < cfg.breakout_conf_vol_x * pb_avg:
                    rejected.append(RejectedSetup(ts, "low_breakout_volume"))
                else:
                    setups.append(MomentumSetup(
                        symbol=symbol,
                        impulse_anchor_ts=anchor_ts,
                        impulse_high=round(impulse_high, 4),
                        anchor_low=round(anchor, 4),
                        pullback_low=round(pullback_low, 4),
                        pullback_bars=pullback_count,
                        confirm_ts=ts,
                        confirm_price=round(b.close, 4),
                        confirm_volume=b.volume or 0.0,
                        stop_price=round(pullback_low, 4),
                        pending_entry=True,
                    ))
            reset()
            idle_start = i + 1
            continue
        pullback_low = min(pullback_low, b.low)
        pullback_vols.append(b.volume or 0.0)
        pullback_count += 1
        if pullback_count > cfg.pullback_max_bars:
            rejected.append(RejectedSetup(ts, "pullback_too_long"))
            reset()
            idle_start = i + 1

    if state == _State.PULLBACK:
        rejected.append(RejectedSetup(bars[-1].timestamp, "incomplete_data_end"))
    elif state == _State.IMPULSE:
        rejected.append(RejectedSetup(bars[-1].timestamp, "impulse_no_pullback_data"))

    return setups, rejected
