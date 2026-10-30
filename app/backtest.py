"""Read-only historical backtest. Never sends broker orders."""
from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from alpaca.data.enums import Adjustment
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from app.market_data import get_data_client, _data_feed, metrics_from_bars
from app.scanner import score_stock

INITIAL_CASH = 1000.0
COST_RATE = 0.001  # 10 bps per side: assumed spread/slippage/fees
ASSUMED_SPREAD_BPS = 10.0  # proxy; NOT historical quotes
HOLD_SESSIONS = 5
WARMUP_SESSIONS = 21


@dataclass(frozen=True)
class ClosedTrade:
    ticker: str
    entry_date: str
    exit_date: str
    gross_pnl: float
    costs: float
    net_pnl: float


@dataclass(frozen=True)
class BacktestResult:
    initial_cash: float
    final_equity: float
    return_pct: float
    benchmark_return_pct: float | None
    max_drawdown_pct: float
    trades: int
    selected_symbols: tuple[str, ...]
    closed_trades: tuple[ClosedTrade, ...] = ()
    total_costs: float = 0.0
    equity_curve: tuple[tuple[str, float], ...] = ()
    position_counts: tuple[tuple[str, int], ...] = ()
    unrealized_pnl: float = 0.0


def fetch_history(symbols: list[str], days: int = 440) -> dict[str, list]:
    """Fetch enough daily bars for a one-year test and a warm-up window."""
    client = get_data_client()
    now = datetime.now(timezone.utc)
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=now - timedelta(days=days),
        end=now - timedelta(days=1),  # never use today's unfinished candle
        adjustment=Adjustment.ALL,
        feed=_data_feed(),
    )
    response = client.get_stock_bars(request)
    data = getattr(response, "data", response)
    return {s: sorted(data.get(s, []), key=lambda b: b.timestamp) for s in symbols}


def _valid_price(value) -> bool:
    return value is not None and value > 0




def run_backtest(history, initial_cash=INITIAL_CASH,
                 hold_sessions=HOLD_SESSIONS, cost_rate=COST_RATE,
                 top_n=3):
    """Daily evaluation, independent positions, no broker orders."""
    if initial_cash <= 0 or hold_sessions < 1 or not 0 <= cost_rate < 1 or top_n < 1:
        raise ValueError("Invalid backtest configuration")
    if "SPY" not in history:
        raise ValueError("SPY history is required")

    spy = {b.timestamp.date(): b for b in history["SPY"]}
    dates = sorted(spy)
    if len(dates) <= WARMUP_SESSIONS + 1:
        raise ValueError("Insufficient SPY history")

    symbols = sorted(s for s in history if s != "SPY")
    lookup = {
        s: {b.timestamp.date(): b for b in history[s]}
        for s in symbols
    }

    cash = initial_cash
    positions = {}
    closed = []
    curve = []
    position_counts = []
    selected = set()
    total_costs = 0.0
    entries = 0
    peak = initial_cash
    max_dd = 0.0

    def sell(symbol, price, date):
        nonlocal cash, total_costs
        pos = positions.pop(symbol)
        shares = pos["shares"]
        proceeds = shares * price
        exit_cost = proceeds * cost_rate
        cash += proceeds - exit_cost
        total_costs += exit_cost

        gross = shares * (price - pos["entry_price"])
        costs = pos["entry_cost"] + exit_cost
        closed.append(ClosedTrade(
            symbol, pos["entry_date"], str(date),
            gross, costs, gross - costs
        ))

    for i in range(WARMUP_SESSIONS, len(dates)):
        day = dates[i]
        signal_day = dates[i - 1]

        # Sell mature positions at today's opening price.
        for symbol, pos in list(positions.items()):
            bar = lookup[symbol].get(day)
            if (
                i - pos["entry_index"] >= hold_sessions
                and bar is not None
                and _valid_price(bar.open)
            ):
                sell(symbol, bar.open, day)

        slots = top_n - len(positions)
        candidates = []

        if slots > 0 and cash > 0:
            for symbol in symbols:
                if symbol in positions:
                    continue

                today = lookup[symbol].get(day)
                if today is None or not _valid_price(today.open):
                    continue

                past = [
                    lookup[symbol][d]
                    for d in dates[:i]
                    if d in lookup[symbol]
                ]
                if len(past) < WARMUP_SESSIONS:
                    continue
                if past[-1].timestamp.date() != signal_day:
                    continue

                metrics = metrics_from_bars(
                    symbol, past, ASSUMED_SPREAD_BPS
                )
                score = score_stock(metrics) if metrics else None
                if score is not None:
                    candidates.append((score, symbol))

            candidates.sort(key=lambda x: (-x[0], x[1]))
            picks = [s for _, s in candidates[:slots]]
            budget = cash / len(picks) if picks else 0

            for symbol in picks:
                price = lookup[symbol][day].open
                shares = budget / (price * (1 + cost_rate))
                entry_cost = shares * price * cost_rate

                cash -= shares * price + entry_cost
                total_costs += entry_cost

                positions[symbol] = {
                    "shares": shares,
                    "entry_price": price,
                    "entry_cost": entry_cost,
                    "entry_date": str(day),
                    "entry_index": i,
                    "last_price": price,
                }
                selected.add(symbol)
                entries += 1

        equity = cash
        for symbol, pos in positions.items():
            bar = lookup[symbol].get(day)
            if bar is not None and _valid_price(bar.close):
                pos["last_price"] = bar.close
            equity += pos["shares"] * pos["last_price"]

        position_counts.append((str(day), len(positions)))
        curve.append((str(day), equity))
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)

    # Do not invent sales at the end of the sample.
    # Keep open positions valued at their last available close.
    final_equity = curve[-1][1]

    open_position_value = sum(
        pos["shares"] * pos["last_price"]
        for pos in positions.values()
    )

    unrealized_pnl = sum(
        pos["shares"] * (pos["last_price"] - pos["entry_price"])
        - pos["entry_cost"]
        for pos in positions.values()
    )

    benchmark_start = spy[dates[WARMUP_SESSIONS]].open
    benchmark_end = spy[dates[-1]].close
    benchmark = (
        (benchmark_end / benchmark_start - 1) * 100
        if _valid_price(benchmark_start)
        and _valid_price(benchmark_end)
        else None
    )

    return BacktestResult(
       initial_cash, final_equity, (final_equity / initial_cash - 1) * 100,
        benchmark, max_dd, entries, tuple(sorted(selected)),
        tuple(closed), total_costs, tuple(curve), tuple(position_counts),
        unrealized_pnl
    )


def backtest_report(result):
    trades = result.closed_trades
    wins = sum(t.net_pnl > 0 for t in trades)
    win_rate = wins / len(trades) * 100 if trades else 0
    gross = sum(t.gross_pnl for t in trades)
    closed_costs = sum(t.costs for t in trades)
    open_entry_costs = result.total_costs - closed_costs
    realized_pnl = sum(t.net_pnl for t in trades)

    by_symbol = defaultdict(float)
    for trade in trades:
        by_symbol[trade.ticker] += trade.net_pnl

    benchmark = (
        f"{result.benchmark_return_pct:+.2f}%"
        if result.benchmark_return_pct is not None else "n/a"
    )
    breakdown = ", ".join(
        f"{symbol}: ${pnl:+.2f}"
        for symbol, pnl in sorted(by_symbol.items())
    ) or "none"

    return (
        "BACKTEST ONLY — V2 simulation; no orders placed\n"
        f"Initial capital:  ${result.initial_cash:,.2f}\n"
        f"Final equity:     ${result.final_equity:,.2f}\n"
        f"Return:           {result.return_pct:+.2f}%\n"
        f"SPY buy & hold:   {benchmark} (before costs)\n"
        f"Max drawdown:     {result.max_drawdown_pct:.2f}%\n"
        f"Entries:          {result.trades}\n"
        f"Closed trades:    {len(trades)}\n"
        f"Win rate:         {win_rate:.1f}%\n"
        f"Closed gross P/L: ${gross:+.2f}\n"
        f"Closed costs:     ${closed_costs:.2f}\n"
        f"Realized P/L:     ${realized_pnl:+.2f}\n"
        f"Unrealized P/L:   ${result.unrealized_pnl:+.2f} (after entry costs)\n"
        f"Open entry costs: ${open_entry_costs:.2f} (already included above)\n"
        f"Total costs:      ${result.total_costs:.2f} (informational)\n"
        f"Net P/L:          ${result.final_equity-result.initial_cash:+.2f}\n"
        f"P/L by symbol:    {breakdown}\n"
        "Assumptions: daily evaluation, max 3 positions, "
        "5-session holding period, 10 bps per side, "
        "assumed 10 bps historical spread. "
        "End-of-sample positions valued at last close. "
        "Experimental strategy."
    )
