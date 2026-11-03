"""H1 — Hold Confirmation evaluator (isolated from the baseline detector).

Rule: after a confirmed breakout at bar N, the next bar N+1 must exist
exactly 60s later AND hold its low at/above the frozen impulse_high.
Entry is simulated at N+2 open via the unchanged simulator (the re-keyed
recovery_ts makes the engine's own contiguous-entry rule enforce N+2).

Uses only data through N+1 — no lookahead. Baseline code untouched.
"""

from dataclasses import dataclass
from datetime import datetime
from app.strategies.momentum_detector import MomentumSetup
from app.strategies.trade_simulator import (
    SimulationResult, TradeConfig, simulate_pullback_trades)


@dataclass(frozen=True)
class _Shim:
    symbol: str | None
    recovery_ts: datetime
    state: str = "recovery_confirmed"


def evaluate_h1(
    bars: list,
    setups: list[MomentumSetup],
    config: TradeConfig | None = None,
    session_times: dict | None = None,
) -> tuple[SimulationResult, dict]:
    """Returns (sim_result, outcomes): outcomes maps confirm_ts ->
    'evaluated' | 'hold_missing_n1' | 'hold_failed' | 'entry_gap_n2'."""
    cfg = config or TradeConfig(require_contiguous_entry=True)
    by_ts = {b.timestamp: i for i, b in enumerate(bars)}
    shims, stops, outcomes = [], {}, {}
    for s in setups:
        i = by_ts.get(s.confirm_ts)
        if i is None or i + 1 >= len(bars):
            outcomes[s.confirm_ts] = "hold_missing_n1"
            continue
        n1 = bars[i + 1]
        if (n1.timestamp - s.confirm_ts).total_seconds() != 60:
            outcomes[s.confirm_ts] = "hold_missing_n1"
            continue
        if n1.low < s.impulse_high:
            outcomes[s.confirm_ts] = "hold_failed"  # decided on data <= N+1
            continue
        # hold passed: engine enters at the bar AFTER N+1 (i.e., N+2)
        shims.append(_Shim(symbol=s.symbol, recovery_ts=n1.timestamp))
        stops[n1.timestamp] = s.stop_price  # original structural stop
        outcomes[s.confirm_ts] = "evaluated"
    sim = simulate_pullback_trades(
        bars, shims, cfg, session_times=session_times, stop_overrides=stops)
    # translate engine skips back to confirm_ts for reporting
    for ts, reason in sim.skipped:
        if reason == "skipped_entry_gap":
            for s in setups:
                i = by_ts.get(s.confirm_ts)
                if i is not None and i + 1 < len(bars) and bars[i + 1].timestamp == ts:
                    outcomes[s.confirm_ts] = "entry_gap_n2"
    return sim, outcomes
