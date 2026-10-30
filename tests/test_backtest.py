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
