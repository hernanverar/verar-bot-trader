"""Six-month barcoding research: oscillation-cycle detection.

Deterministic, non-overlapping, lookahead-free. A *leg* is a directional
move confirmed only when price retraces the threshold against it — the
pivot is always identified after the fact at the confirmation bar, so
leg confirmations are the only events knowable in real time.

A *cycle* is a pair of consecutive completed legs (one up, one down).
Legs alternate by construction and new legs start at each confirmation
bar, so cycles never overlap and never share the same data twice.
"""

from dataclasses import dataclass
from datetime import datetime

from app.market_data import MARKET_TZ


@dataclass(frozen=True)
class Leg:
    direction: str            # up | down
    start_ts: datetime
    start_price: float
    pivot_ts: datetime        # the extreme bar — known only in hindsight
    pivot_price: float
    confirm_ts: datetime      # bar where the retracement confirmed the leg
    confirm_price: float


@dataclass(frozen=True)
class Cycle:
    symbol: str
    threshold_pct: float
    session_date: str
    start_ts: datetime
    end_ts: datetime          # second leg's confirmation timestamp
    start_price: float
    pivot_price: float
    end_price: float          # confirmation price of second leg
    amplitude_pct: float      # |leg1 %| + |leg2 %|
    duration_min: float
    confirm_lag_bars_leg1: int
    confirm_lag_bars_leg2: int


def detect_legs(bars: list, threshold: float) -> list[tuple[Leg, int]]:
    """Return (leg, confirm_bar_index) pairs in chronological order."""
    t = threshold / 100
    legs = []
    direction = None
    extreme = None
    anchor_ts = anchor_price = extreme_ts = None

    for i, b in enumerate(bars):
        c = b.close
        if c <= 0:
            continue
        if direction is None:
            anchor_ts, anchor_price = b.timestamp, c
            extreme, extreme_ts = c, b.timestamp
            direction = "flat"
            continue
        if direction == "flat":
            if c >= anchor_price * (1 + t):
                direction, extreme, extreme_ts = "up", c, b.timestamp
            elif c <= anchor_price * (1 - t):
                direction, extreme, extreme_ts = "down", c, b.timestamp
            continue
        if direction == "up":
            if c > extreme:
                extreme, extreme_ts = c, b.timestamp
            elif c <= extreme * (1 - t):
                legs.append((Leg("up", anchor_ts, anchor_price, extreme_ts,
                                 extreme, b.timestamp, c), i))
                anchor_ts, anchor_price = extreme_ts, extreme  # next leg starts at pivot
                direction, extreme, extreme_ts = "down", c, b.timestamp
        else:  # down
            if c < extreme:
                extreme, extreme_ts = c, b.timestamp
            elif c >= extreme * (1 + t):
                legs.append((Leg("down", anchor_ts, anchor_price, extreme_ts,
                                 extreme, b.timestamp, c), i))
                anchor_ts, anchor_price = extreme_ts, extreme
                direction, extreme, extreme_ts = "up", c, b.timestamp
    return legs


def count_cycles(
    bars: list,
    threshold_pct: float,
    symbol: str = "",
) -> list[Cycle]:
    """Non-overlapping completed up/down oscillation cycles."""
    legs = detect_legs(bars, threshold_pct)
    cycles = []
    # Non-overlapping pairs: legs [L1, L2, L3, L4] -> cycles (L1,L2), (L3,L4).
    for k in range(0, len(legs) - 1, 2):
        leg1, conf1_idx = legs[k]
        leg2, conf2_idx = legs[k + 1]
        amp = abs(leg1.pivot_price / leg1.start_price - 1) + abs(
            leg2.pivot_price / leg2.start_price - 1
        )
        cycles.append(
            Cycle(
                symbol=symbol,
                threshold_pct=threshold_pct,
                session_date=leg1.start_ts.astimezone(MARKET_TZ).date().isoformat(),
                start_ts=leg1.start_ts,
                end_ts=leg2.confirm_ts,
                start_price=leg1.start_price,
                pivot_price=leg1.pivot_price,
                end_price=leg2.confirm_price,
                amplitude_pct=round(amp * 100, 2),
                duration_min=(leg2.confirm_ts - leg1.start_ts).total_seconds() / 60,
                confirm_lag_bars_leg1=conf1_idx,
                confirm_lag_bars_leg2=conf2_idx,
            )
        )
    return cycles


def daily_ranges(bars: list) -> dict[str, float]:
    """{session_date: (high-low)/low * 100} per ET session."""
    sessions: dict[str, tuple[float, float]] = {}
    for b in bars:
        d = b.timestamp.astimezone(MARKET_TZ).date().isoformat()
        hi, lo = sessions.get(d, (0.0, float("inf")))
        sessions[d] = (max(hi, b.high), min(lo, b.low))
    return {d: round((h - l) / l * 100, 2) for d, (h, l) in sessions.items() if l > 0}
