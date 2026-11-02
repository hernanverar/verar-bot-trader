"""Deterministic intraday pullback trade simulator. Research-only — no orders.

Evaluates `recovery_confirmed` pullback patterns over completed regular-
session 1-minute bars:

- Entry: next completed regular-session bar's open after confirmation
  (never the confirming bar itself, never across sessions/gaps).
- Stop: entry*(1-stop_pct). Profit target: entry*(1+target_pct).
- Exit: first bar whose low reaches stop or high reaches target; if both
  in one bar the stop is assumed hit (conservative). Opening gaps through
  a level fill at the bar open, not the level — a stop cannot fill better
  than the gap, and a target fill takes the favorable open.
- Session end: positions never held overnight; unfilled exits close at
  the final regular-session bar's close (documented assumption).
- Sizing: whole shares only, no leverage — min(shares by 1% risk budget,
  shares affordable with cash). Slippage/commission applied to both
  sides; the 1% budget is not a loss cap guarantee — gaps can exceed it.
- Single position at a time; equity and risk budgets update per trade.

Limitations: minute bars give OHLC only — intrabar path is unknowable,
so same-bar ambiguity resolves to the conservative (stop) outcome.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from app.market_data import MARKET_TZ
from app.strategies.pullback_detector import PullbackPattern, RECOVERY_CONFIRMED


@dataclass(frozen=True)
class TradeConfig:
    initial_equity: float = 2000.0
    risk_pct: float = 0.01
    stop_loss_pct: float = 5.0
    profit_target_pct: float = 15.0
    entry_slippage_bps: float = 5.0
    exit_slippage_bps: float = 5.0
    commission_per_trade: float = 0.0
    regular_close: time = time(16, 0)
    max_gap_minutes: int = 3  # same continuity rule as the detector


@dataclass(frozen=True)
class SimulatedTrade:
    symbol: str
    recovery_ts: datetime
    entry_ts: datetime
    entry_price: float
    stop_price: float
    target_price: float
    shares: int
    risk_budget: float
    exit_ts: datetime
    exit_price: float
    exit_reason: str  # stop | target | session_end
    gross_pnl: float
    net_pnl: float
    equity_after: float


@dataclass(frozen=True)
class SimulationResult:
    symbol: str
    config: TradeConfig
    patterns_confirmed: int
    trades: tuple[SimulatedTrade, ...]
    skipped: tuple[tuple[datetime, str], ...]
    win_rate: float | None
    gross_pnl: float
    net_pnl: float
    ending_equity: float
    max_drawdown: float
    exit_reasons: dict[str, int]


def _exit_price(level: float, slip: float) -> float:
    return level * (1 - slip)


def _segment_end(bars: list, start_idx: int, max_gap_minutes: int) -> int:
    """Last bar index of the session segment containing `start_idx`.

    Mirrors the detector's rule: a session-date change or a timestamp gap
    larger than the configured maximum ends the segment. Trades never
    cross that boundary.
    """
    tz = MARKET_TZ
    day = bars[start_idx].timestamp.astimezone(tz).date()
    end = start_idx
    for k in range(start_idx + 1, len(bars)):
        b = bars[k]
        if (
            b.timestamp.astimezone(tz).date() != day
            or b.timestamp - bars[k - 1].timestamp > timedelta(minutes=max_gap_minutes)
        ):
            break
        end = k
    return end


def simulate_pullback_trades(
    bars: list,
    patterns: list[PullbackPattern],
    config: TradeConfig | None = None,
) -> SimulationResult:
    """Simulate trades for confirmed patterns over regular-session bars."""
    config = config or TradeConfig()
    symbol = patterns[0].symbol if patterns else (getattr(bars[0], "symbol", "") if bars else "")
    confirmed = [p for p in patterns if p.state == RECOVERY_CONFIRMED]

    by_ts = {b.timestamp: i for i, b in enumerate(bars)}
    slip_in = config.entry_slippage_bps / 10_000
    slip_out = config.exit_slippage_bps / 10_000

    equity = config.initial_equity
    trades: list[SimulatedTrade] = []
    skipped: list[tuple[datetime, str]] = []
    exit_reasons: dict[str, int] = {}
    equity_curve = [equity]
    busy_until = -1  # single-position constraint

    for pattern in confirmed:
        rec_idx = by_ts.get(pattern.recovery_ts)
        if rec_idx is None:
            skipped.append((pattern.recovery_ts, "confirmation bar not in data"))
            continue
        entry_idx = rec_idx + 1
        if entry_idx >= len(bars):
            skipped.append((pattern.recovery_ts, "no next regular-session bar"))
            continue
        if entry_idx <= busy_until:
            skipped.append((pattern.recovery_ts, "position already open"))
            continue

        entry_bar = bars[entry_idx]
        seg_end = _segment_end(bars, rec_idx, config.max_gap_minutes)
        if entry_idx > seg_end:
            skipped.append((pattern.recovery_ts, "no next regular-session bar"))
            continue
        entry = entry_bar.open * (1 + slip_in)
        stop = entry * (1 - config.stop_loss_pct / 100)
        target = entry * (1 + config.profit_target_pct / 100)
        risk_per_share = entry - stop
        budget = equity * config.risk_pct

        shares = min(
            int(budget // risk_per_share) if risk_per_share > 0 else 0,
            int(equity // entry),
        )
        if shares <= 0:
            skipped.append(
                (pattern.recovery_ts, "insufficient risk budget or buying power")
            )
            continue

        exit_idx, exit_price, reason = None, None, None
        for k in range(entry_idx, seg_end + 1):
            b = bars[k]
            if b.open <= stop:
                exit_idx, exit_price, reason = k, _exit_price(b.open, slip_out), "stop"
            elif b.low <= stop:
                exit_idx, exit_price, reason = k, _exit_price(stop, slip_out), "stop"
            elif b.open >= target:
                exit_idx, exit_price, reason = k, _exit_price(b.open, slip_out), "target"
            elif b.high >= target:
                exit_idx, exit_price, reason = k, _exit_price(target, slip_out), "target"
            if exit_idx is not None:
                break
        if exit_idx is None:
            # Documented assumption: liquidate at the final regular-session
            # bar's close — no overnight positions, no invented fills.
            exit_idx = seg_end
            exit_price = _exit_price(bars[exit_idx].close, slip_out)
            reason = "session_end"

        gross = (exit_price - entry) * shares
        net = gross - 2 * config.commission_per_trade
        equity += net
        equity_curve.append(equity)
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        busy_until = exit_idx

        trades.append(
            SimulatedTrade(
                symbol=pattern.symbol or symbol,
                recovery_ts=pattern.recovery_ts,
                entry_ts=entry_bar.timestamp,
                entry_price=round(entry, 4),
                stop_price=round(stop, 4),
                target_price=round(target, 4),
                shares=shares,
                risk_budget=round(budget, 2),
                exit_ts=bars[exit_idx].timestamp,
                exit_price=round(exit_price, 4),
                exit_reason=reason,
                gross_pnl=round(gross, 2),
                net_pnl=round(net, 2),
                equity_after=round(equity, 2),
            )
        )

    wins = sum(1 for t in trades if t.net_pnl > 0)
    peak = config.initial_equity
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100 if peak else 0.0)

    return SimulationResult(
        symbol=symbol,
        config=config,
        patterns_confirmed=len(confirmed),
        trades=tuple(trades),
        skipped=tuple(skipped),
        win_rate=round(wins / len(trades) * 100, 2) if trades else None,
        gross_pnl=round(sum(t.gross_pnl for t in trades), 2),
        net_pnl=round(sum(t.net_pnl for t in trades), 2),
        ending_equity=round(equity, 2),
        max_drawdown=round(max_dd, 2),
        exit_reasons=exit_reasons,
    )
