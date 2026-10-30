from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.main import app
from app.backtest import BacktestResult
from app.strategies.hot_stock_scanner import HotStock


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


def test_hot_stocks_endpoint():
    stock = HotStock(
        symbol="NVDA",
        last_price=200.0,
        change_pct=0.05,
        relative_volume=3.0,
        cum_volume=5_000_000,
        dollar_volume=1e9,
        momentum_pct=0.01,
        spread_bps=15.0,
        score=72.4,
        classification="eligible",
        reasons=(),
        bar_freshness="fresh",
        quote_available=True,
        as_of=datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc),
    )
    filtered = HotStock(
        symbol="TSLA",
        last_price=250.0,
        change_pct=-0.02,
        relative_volume=0.8,
        cum_volume=2_000_000,
        dollar_volume=5e8,
        momentum_pct=-0.01,
        spread_bps=20.0,
        score=15.0,
        classification="filtered",
        reasons=("change -2.00% below +3.00% minimum",),
        bar_freshness="fresh",
        quote_available=True,
        as_of=datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc),
    )

    with patch(
        "app.api.main.scan_hot_stocks", return_value=[stock, filtered]
    ) as scan:
        response = client.get("/api/scanner/hot-stocks")

    assert response.status_code == 200
    data = response.json()
    assert "as_of" in data
    assert len(data["results"]) == 2
    assert data["total_scanned"] == 2
    assert data["eligible_count"] == 1
    assert data["universe"] == ["NVDA", "TSLA", "GME", "AMC", "AAPL", "MSFT"]

    row = data["results"][0]
    assert row["symbol"] == "NVDA"
    assert row["last_price"] == 200.0
    assert row["change_pct"] == 0.05
    assert row["relative_volume"] == 3.0
    assert row["dollar_volume"] == 1e9
    assert row["momentum_pct"] == 0.01
    assert row["score"] == 72.4
    assert row["classification"] == "eligible"
    assert row["reasons"] == []
    assert row["bar_freshness"] == "fresh"
    assert row["quote_available"] is True
    assert row["as_of"] is not None

    filtered_row = data["results"][1]
    assert filtered_row["classification"] == "filtered"
    assert filtered_row["reasons"] == ["change -2.00% below +3.00% minimum"]

    scan.assert_called_once()


def test_hot_stocks_passes_symbols_and_config():
    with patch("app.api.main.scan_hot_stocks", return_value=[]) as scan:
        response = client.get(
            "/api/scanner/hot-stocks?symbols=nvda,tsla&top=5&min_rvol=1.5"
        )

    assert response.status_code == 200
    args, kwargs = scan.call_args
    assert args[0] == ["NVDA", "TSLA"]
    assert kwargs["config"].min_rvol == 1.5
    assert response.json()["results"] == []


def test_hot_stocks_rejects_invalid_symbols():
    response = client.get("/api/scanner/hot-stocks?symbols=NVDA,***")
    assert response.status_code == 422

    response = client.get("/api/scanner/hot-stocks?top=0")
    assert response.status_code == 422