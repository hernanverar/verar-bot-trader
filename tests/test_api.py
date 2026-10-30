from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.main import app
from app.backtest import BacktestResult


client = TestClient(app)


def test_health_check():
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "verar-bot-trader",
        "version": "0.1.0",
        "trading_mode": "paper",
    }


def test_backtest_endpoint():
    mock_result = BacktestResult(
        initial_cash=1000.0,
        final_equity=1050.0,
        return_pct=5.0,
        benchmark_return_pct=3.0,
        max_drawdown_pct=2.0,
        trades=2,
        selected_symbols=("AAPL", "MSFT"),
        benchmark_equity_curve=(
            ("2026-01-05", 1000.0),
            ("2026-01-06", 1005.0),
        ),
    )

    with (
        patch("app.api.main.fetch_history", return_value={"SPY": []}) as fetch,
        patch("app.api.main.run_backtest", return_value=mock_result) as run,
    ):
        response = client.post(
            "/api/backtest",
            json={
                "initial_cash": 1000,
                "hold_sessions": 5,
                "top_n": 3,
            },
        )

    assert response.status_code == 200

    data = response.json()
    assert data["final_equity"] == 1050.0
    assert data["return_pct"] == 5.0
    assert data["selected_symbols"] == ["AAPL", "MSFT"]
    assert data["benchmark_equity_curve"] == [
        ["2026-01-05", 1000.0],
        ["2026-01-06", 1005.0],
    ]

    fetch.assert_called_once()
    run.assert_called_once_with(
        {"SPY": []},
        initial_cash=1000.0,
        hold_sessions=5,
        top_n=3,
    )


def test_backtest_rejects_invalid_parameters():
    response = client.post(
        "/api/backtest",
        json={"initial_cash": -100},
    )

    assert response.status_code == 422