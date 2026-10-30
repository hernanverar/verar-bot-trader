import pytest

from app.backtest import BacktestResult, ClosedTrade, backtest_report


def test_trade_net_pnl_equals_gross_minus_costs():
    trade = ClosedTrade(
        ticker="AAPL",
        entry_date="2026-01-05",
        exit_date="2026-01-12",
        gross_pnl=15.0,
        costs=2.0,
        net_pnl=13.0,
    )

    assert trade.net_pnl == pytest.approx(
        trade.gross_pnl - trade.costs
    )


def test_report_reconciles_net_profit():
    trades = (
        ClosedTrade("AAPL", "2026-01-05", "2026-01-12", 20, 2, 18),
        ClosedTrade("TSLA", "2026-01-06", "2026-01-13", -10, 2, -12),
    )

    result = BacktestResult(
        initial_cash=1000.0,
        final_equity=1006.0,
        return_pct=0.6,
        benchmark_return_pct=2.0,
        max_drawdown_pct=1.0,
        trades=2,
        selected_symbols=("AAPL", "TSLA"),
        closed_trades=trades,
        total_costs=4.0,
    )

    assert sum(t.net_pnl for t in trades) == pytest.approx(
        result.final_equity - result.initial_cash
    )
    assert sum(t.costs for t in trades) == pytest.approx(
        result.total_costs
    )

    report = backtest_report(result)

    assert "Total costs:" in report
    assert "Closed costs:" in report
    assert "Realized P/L:" in report
    assert "Unrealized P/L:" in report
    assert "Net P/L:" in report
    assert "$+6.00" in report


def test_flat_prices_lose_only_execution_costs():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from app.backtest import run_backtest

    start = datetime(2026, 1, 5, tzinfo=timezone.utc)

    def make_bars(price, volume):
        return [
            SimpleNamespace(
                timestamp=start + timedelta(days=i),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
            )
            for i in range(40)
        ]

    history = {
        "SPY": make_bars(500, 10_000_000),
        "AAPL": make_bars(100, 1_000_000),
    }

    result = run_backtest(history)

    assert result.trades > 0
    assert result.final_equity < result.initial_cash
    assert result.total_costs > 0

    assert result.final_equity == pytest.approx(
        result.initial_cash - result.total_costs,
        abs=0.01,
    )

    # Positions still open at end-of-sample are valued at last close,
    # so their unrealized P/L is part of the equity change.
    assert sum(t.net_pnl for t in result.closed_trades) + result.unrealized_pnl == pytest.approx(
        result.final_equity - result.initial_cash,
        abs=0.01,
    )


def test_maximum_three_simultaneous_positions():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from app.backtest import run_backtest

    start = datetime(2026, 1, 5, tzinfo=timezone.utc)

    def make_bars(price):
        return [
            SimpleNamespace(
                timestamp=start + timedelta(days=i),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1_000_000,
            )
            for i in range(45)
        ]

    history = {"SPY": make_bars(500)}
    for ticker in ["AAPL", "MSFT", "NVDA", "TSLA", "AMD"]:
        history[ticker] = make_bars(100)

    result = run_backtest(history, top_n=3)

    # With equal scores, alphabetical tie-breaking selects
    # the same three symbols for this constant-price fixture.
    assert set(result.selected_symbols) == {"AAPL", "AMD", "MSFT"}
    assert result.trades > 0


def test_position_count_never_exceeds_limit():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from app.backtest import run_backtest

    start = datetime(2026, 1, 5, tzinfo=timezone.utc)

    def bars(price):
        return [
            SimpleNamespace(
                timestamp=start + timedelta(days=i),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1_000_000,
            )
            for i in range(50)
        ]

    history = {"SPY": bars(500)}
    for ticker in ["AAPL", "AMD", "MSFT", "NVDA", "TSLA"]:
        history[ticker] = bars(100)

    result = run_backtest(history, top_n=3)

    assert result.position_counts
    assert all(
        0 <= count <= 3
        for _, count in result.position_counts
    )
    assert max(count for _, count in result.position_counts) == 3


def test_future_prices_do_not_change_past_equity():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from app.backtest import run_backtest

    start = datetime(2026, 1, 5, tzinfo=timezone.utc)

    def make_bars(price, future_multiplier=1.0):
        bars = []

        for i in range(55):
            current_price = price * (1 + i * 0.002)

            # Change only data from session 40 onward.
            if i >= 40:
                current_price *= future_multiplier

            bars.append(
                SimpleNamespace(
                    timestamp=start + timedelta(days=i),
                    open=current_price,
                    high=current_price,
                    low=current_price,
                    close=current_price,
                    volume=1_000_000,
                )
            )
def test_closed_trades_respect_holding_period():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from app.backtest import run_backtest

    start = datetime(2026, 1, 5, tzinfo=timezone.utc)

    dates = [
        (start + timedelta(days=i)).date()
        for i in range(60)
    ]

    def make_bars(price):
        return [
            SimpleNamespace(
                timestamp=datetime.combine(
                    day, datetime.min.time(), tzinfo=timezone.utc
                ),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1_000_000,
            )
            for day in dates
        ]

    history = {
        "SPY": make_bars(500),
        "AAPL": make_bars(100),
        "MSFT": make_bars(200),
    }


def test_missing_exit_price_delays_sale():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from app.backtest import run_backtest

    start = datetime(2026, 1, 5, tzinfo=timezone.utc)

    def make_bars(price, missing_days=None):
        missing_days = missing_days or set()
        bars = []

        for i in range(45):
            if i in missing_days:
                continue

            bars.append(
                SimpleNamespace(
                    timestamp=start + timedelta(days=i),
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=1_000_000,
                )
            )

        return bars

    history = {
        "SPY": make_bars(500),
        "AAPL": make_bars(100, missing_days={26}),
    }

    result = run_backtest(history, hold_sessions=5)

    assert result.closed_trades

    first_trade = result.closed_trades[0]

    assert first_trade.entry_date == "2026-01-26"
    assert first_trade.exit_date == "2026-02-01"
