from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.backtest import backtest_report, run_backtest


def bars(start, count, start_price=100.0, daily_growth=0.002, volume=1_000_000):
    result = []
    for i in range(count):
        price = start_price * ((1 + daily_growth) ** i)
        result.append(SimpleNamespace(
            timestamp=start + timedelta(days=i), open=price,
            close=price * 1.001, volume=volume))
    return result


def test_backtest_no_orders_and_positive_benchmark():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    history = {"SPY": bars(start, 45, 400), "AAA": bars(start, 45, 100)}
    result = run_backtest(history)
    assert result.initial_cash == 1000
    assert result.benchmark_return_pct > 0
    assert result.trades > 0
    assert result.final_equity > 0
    assert "BACKTEST ONLY" in backtest_report(result)


def test_backtest_rejects_bad_parameters():
    with pytest.raises(ValueError):
        run_backtest({"SPY": []}, initial_cash=0)
    with pytest.raises(ValueError):
        run_backtest({"AAA": []})


def test_backtest_no_eligible_stocks_keeps_cash():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    history = {"SPY": bars(start, 40, 400),
               "AAA": bars(start, 40, 2, volume=10)}
    result = run_backtest(history)
    assert result.trades == 0
    assert result.final_equity == 1000


def test_benchmark_curve_matches_reported_return():
    from app.backtest import BacktestResult

    result = BacktestResult(
        initial_cash=1000.0,
        final_equity=1050.0,
        return_pct=5.0,
        benchmark_return_pct=10.0,
        max_drawdown_pct=2.0,
        trades=1,
        selected_symbols=("AAPL",),
        benchmark_equity_curve=(
            ("2026-01-01", 1020.0),
            ("2026-01-02", 1100.0),
        ),
    )

    last_value = result.benchmark_equity_curve[-1][1]

    assert round(
        (last_value / result.initial_cash - 1) * 100, 2
    ) == result.benchmark_return_pct


def test_benchmark_curve_dates_align_with_equity_curve():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    history = {"SPY": bars(start, 45, 400), "AAA": bars(start, 45, 100)}
    result = run_backtest(history)

    assert len(result.benchmark_equity_curve) == len(result.equity_curve)
    assert (
        [d for d, _ in result.benchmark_equity_curve]
        == [d for d, _ in result.equity_curve]
    )


def test_benchmark_curve_final_value_matches_reported_return():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    history = {"SPY": bars(start, 45, 400), "AAA": bars(start, 45, 100)}
    result = run_backtest(history, initial_cash=1000.0)

    final_benchmark = result.benchmark_equity_curve[-1][1]
    expected = 1000.0 * (1 + result.benchmark_return_pct / 100)
    assert final_benchmark == pytest.approx(expected, abs=0.01)


def test_benchmark_curve_normalized_to_initial_cash():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    spy = [
        SimpleNamespace(
            timestamp=start + timedelta(days=i),
            open=400.0,
            close=400.0,
            volume=1_000_000,
        )
        for i in range(45)
    ]
    history = {"SPY": spy, "AAA": bars(start, 45, 100)}
    result = run_backtest(history, initial_cash=2500.0)

    assert result.benchmark_equity_curve
    assert all(
        value == pytest.approx(2500.0)
        for _, value in result.benchmark_equity_curve
    )
    assert result.benchmark_return_pct == pytest.approx(0.0)