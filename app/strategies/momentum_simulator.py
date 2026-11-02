"""Momentum-setup → simulator adapter.

Separates setup confirmation from entry resolution and outcome:
confirmed MomentumSetup events feed the existing simulate_pullback_trades
engine with structural stops (pullback low) and risk-reward targets —
no duplicated exit/sizing/slippage logic.
"""

from dataclasses import dataclass
from datetime import datetime

from app.strategies.momentum_detector import MomentumSetup
from app.strategies.trade_simulator import SimulationResult
from app.strategies.trade_simulator import (
    TradeConfig,
    simulate_pullback_trades,
)


@dataclass(frozen=True)
class _PatternShim:
    """Minimal pattern shape the simulator consumes."""
    symbol: str | None
    recovery_ts: datetime
    state: str = "recovery_confirmed"


def lifecycle_status(confirm_ts, sim: SimulationResult) -> str:
    """Derive a setup's outcome from actual simulator outputs.

    confirmed_pending_entry | simulated_filled | skipped_invalid_risk
    | skipped_no_next_bar | unresolved_data_end
    """
    if any(t.recovery_ts == confirm_ts for t in sim.trades):
        return "simulated_filled"
    if any(t.recovery_ts == confirm_ts for t in sim.unresolved):
        return "unresolved_data_end"
    for ts, reason in sim.skipped:
        if ts == confirm_ts:
            if reason.startswith("skipped_"):
                return reason
            if "risk geometry" in reason:
                return "skipped_invalid_risk"
            if "no next" in reason:
                return "skipped_no_next_bar"
            return f"skipped_{reason.split()[0]}"
    return "confirmed_pending_entry"


def simulate_momentum_setups(
    bars: list,
    setups: list[MomentumSetup],
    config: TradeConfig | None = None,
    session_times: dict | None = None,
) -> SimulationResult:
    """Simulate entries for CONFIRMED setups — pending until next bar.

    Stop = structural pullback low (stop_price on the setup). Target =
    entry + risk_reward * (entry - stop) via TradeConfig.risk_reward —
    computed from the actual simulated entry, never pre-resolved.
    Entries still obey the simulator's next-bar rule; no same-bar fills.
    """
    cfg = config or TradeConfig(require_contiguous_entry=True)
    patterns = [
        _PatternShim(symbol=s.symbol, recovery_ts=s.confirm_ts) for s in setups
    ]
    stops = {s.confirm_ts: s.stop_price for s in setups}
    return simulate_pullback_trades(
        bars, patterns, cfg, session_times=session_times, stop_overrides=stops
    )
