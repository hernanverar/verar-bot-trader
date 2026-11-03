from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.main import app
from app.backtest import BacktestResult
from app.market_discovery import DiscoveryResult
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
        research_eligible=True,
        ross_eligible=False,
        ross_fails=("daily gain below +10%", "rvol below 5x"),
        ross_unverified=("float <10M (no reliable source)", "news catalyst (no source wired)"),
        bar_status="live",
        bar_age_min=1.0,
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
        research_eligible=False,
        ross_eligible=False,
        ross_fails=(),
        ross_unverified=(),
        bar_status="live",
        bar_age_min=1.0,
        bar_freshness="fresh",
        quote_available=True,
        as_of=datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc),
    )

    with patch(
        "app.api.main.scan_hot_stocks", return_value=[stock, filtered]
    ) as scan:
        response = client.get("/api/scanner/hot-stocks?mode=manual")

    assert response.status_code == 200
    data = response.json()
    assert "as_of" in data
    assert len(data["results"]) == 2
    assert data["total_scanned"] == 2
    assert data["eligible_count"] == 1
    assert data["discovery_mode"] == "manual"
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
    # Explicit symbols imply manual mode for backward compatibility.
    assert response.json()["discovery_mode"] == "manual"


def test_hot_stocks_rejects_invalid_symbols():
    response = client.get("/api/scanner/hot-stocks?symbols=NVDA,***")
    assert response.status_code == 422

    response = client.get("/api/scanner/hot-stocks?top=0")
    assert response.status_code == 422


def test_hot_stocks_auto_mode_discovers_universe():
    discovered = DiscoveryResult(
        symbols=("MARA", "RIOT"),
        source="screener:movers+actives",
        as_of=datetime(2026, 1, 15, 15, 30, tzinfo=timezone.utc),
        is_fallback=False,
        warning=None,
    )
    with (
        patch("app.api.main.get_trading_client", return_value=object()),
        patch(
            "app.api.main.discover_momentum_universe",
            return_value=discovered,
        ) as discover,
        patch("app.api.main.scan_hot_stocks", return_value=[]) as scan,
    ):
        response = client.get("/api/scanner/hot-stocks?mode=auto")

    assert response.status_code == 200
    data = response.json()
    assert data["discovery_mode"] == "auto"
    assert data["discovery_source"] == "screener:movers+actives"
    assert data["discovery_as_of"] == "2026-01-15T15:30:00+00:00"
    assert data["is_fallback"] is False
    assert data["discovery_warning"] is None
    assert data["universe"] == ["MARA", "RIOT"]

    discover.assert_called_once()
    scan.assert_called_once()
    assert scan.call_args[0][0] == ["MARA", "RIOT"]


def test_hot_stocks_auto_fallback_is_reported():
    discovered = DiscoveryResult(
        symbols=("NVDA", "TSLA"),
        source="fallback_curated",
        as_of=datetime(2026, 1, 15, 15, 30, tzinfo=timezone.utc),
        is_fallback=True,
        warning="Screener discovery unavailable; NOT market-wide discovery.",
    )
    with (
        patch("app.api.main.get_trading_client", return_value=object()),
        patch(
            "app.api.main.discover_momentum_universe",
            return_value=discovered,
        ),
        patch("app.api.main.scan_hot_stocks", return_value=[]),
    ):
        response = client.get("/api/scanner/hot-stocks?mode=auto")

    assert response.status_code == 200
    data = response.json()
    assert data["is_fallback"] is True
    assert data["discovery_source"] == "fallback_curated"
    assert "NOT market-wide" in data["discovery_warning"]


def test_hot_stocks_rejects_unknown_mode():
    response = client.get("/api/scanner/hot-stocks?mode=wat")
    assert response.status_code == 422


def test_pullbacks_endpoint_returns_patterns():
    from types import SimpleNamespace

    fake_bar = SimpleNamespace(
        symbol="AAPL", timestamp=datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc),
        open=100.0, high=100.5, low=99.9, close=100.4, volume=1000,
    )
    with (
        patch("app.api.main.get_data_client", return_value=object()),
        patch("app.api.main.fetch_minute_bars", return_value={"AAPL": [fake_bar]}),
        patch("app.api.main.get_trading_client", return_value=object()),
        patch("app.api.main._fetch_trading_days",
              return_value={fake_bar.timestamp.date(): (datetime(2026,1,15,9,30).time(), datetime(2026,1,15,16,0).time())}),
        patch("app.api.main.detect_pullbacks") as detect,
    ):
        detect.return_value = []
        response = client.get("/api/strategies/pullbacks?symbol=aapl")

    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "AAPL"
    assert data["session_verified"] is True
    assert data["bars_analyzed"] == 1
    assert data["patterns_found"] == 0
    assert "as_of" in data
    detect.assert_called_once()


def test_pullbacks_validation():
    assert client.get("/api/strategies/pullbacks?symbol=AAPL&retrace_min=0.8&retrace_max=0.2").status_code == 422
    assert client.get("/api/strategies/pullbacks?symbol=***").status_code == 422
    assert client.get("/api/strategies/pullbacks?symbol=AAPL&impulse_min_bars=50&impulse_max_bars=10").status_code == 422
    assert client.get("/api/strategies/pullbacks").status_code == 422  # symbol required


def test_auto_mode_accepts_alpaca_symbol_formats():
    """Regression: discovered tickers like BRK.B must not trip the
    manual-input 'Invalid symbols list' validation."""
    discovered = DiscoveryResult(
        symbols=("BRK.B", "MARA"),
        source="screener:movers",
        as_of=datetime(2026, 1, 15, 15, 30, tzinfo=timezone.utc),
        is_fallback=False,
        warning=None,
    )
    with (
        patch("app.api.main.get_trading_client", return_value=object()),
        patch("app.api.main.discover_momentum_universe", return_value=discovered),
        patch("app.api.main.scan_hot_stocks", return_value=[]) as scan,
    ):
        response = client.get("/api/scanner/hot-stocks?mode=auto")

    assert response.status_code == 200
    data = response.json()
    assert data["universe"] == ["BRK.B", "MARA"]
    scan.assert_called_once()
    assert scan.call_args[0][0] == ["BRK.B", "MARA"]


def test_auto_empty_discovery_is_not_a_422():
    discovered = DiscoveryResult(
        symbols=(),
        source="screener:movers",
        as_of=datetime(2026, 1, 15, 15, 30, tzinfo=timezone.utc),
        is_fallback=False,
        warning=None,
    )
    with (
        patch("app.api.main.get_trading_client", return_value=object()),
        patch("app.api.main.discover_momentum_universe", return_value=discovered),
        patch("app.api.main.scan_hot_stocks", return_value=[]) as scan,
    ):
        response = client.get("/api/scanner/hot-stocks?mode=auto")

    assert response.status_code == 200
    data = response.json()
    assert data["universe"] == []
    assert data["results"] == []
    assert data["discovery_warning"]
    scan.assert_not_called()

def test_chart_bars_endpoint():
    from types import SimpleNamespace

    now = datetime.now(timezone.utc)
    fake_bars = [
        SimpleNamespace(
            timestamp=now.replace(minute=0) - timedelta(hours=2),
            open=100.0, high=101.0, low=99.0, close=100.5, volume=5000,
        )
    ]

    class FakeResult:
        data = {"AAPL": fake_bars}

    with (
        patch("app.api.main.get_data_client") as gdc,
        patch.object(gdc.return_value, "get_stock_bars", return_value=FakeResult()),
    ):
        response = client.get("/api/charts/bars?symbol=aapl&interval=1")

    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "AAPL"
    assert data["interval_min"] == 1
    assert data["feed"]
    assert len(data["bars"]) == 1
    assert data["bars"][0]["close"] == 100.5


def test_chart_bars_validation():
    assert client.get("/api/charts/bars?symbol=AAPL&interval=7").status_code == 422
    assert client.get("/api/charts/bars?symbol=***").status_code == 422
    assert client.get("/api/charts/bars").status_code == 422


def test_alerts_evaluate_endpoint_no_crash(monkeypatch):
    """Regression: /api/alerts/evaluate must not 500 on NameError.

    The Phase 2D instrumentation referenced t_eval inside the store
    mutation closure — outside its scope — 500ing every evaluation,
    which silently prevented alert state persistence and firing."""
    import app.alert_store as store
    from fastapi.testclient import TestClient

    monkeypatch.setattr(store, "load_alerts", lambda: [])
    monkeypatch.setattr(store, "load_events", lambda: [])
    monkeypatch.setattr(store, "mutate", lambda fn: fn([], [])[0])
    client = TestClient(app)
    res = client.post("/api/alerts/evaluate")
    assert res.status_code == 200
    body = res.json()
    assert body["new_events"] == []
    assert not body.get("busy")


def test_evaluate_exception_records_api_error(monkeypatch):
    """An internal failure in evaluation must surface as api_error diagnostic
    while still returning HTTP 500 to the caller."""
    import app.api.main as api_main
    import app.alert_store as store
    from app.diagnostics import load_day
    from datetime import datetime
    from zoneinfo import ZoneInfo

    records = []
    monkeypatch.setattr(api_main, "_diag", lambda kind, **kw: records.append(kind))
    monkeypatch.setattr(
        api_main, "_evaluate_alerts_locked",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    res = TestClient(app, raise_server_exceptions=False).post("/api/alerts/evaluate")
    assert res.status_code == 500
    assert "api_error" in records


def test_momentum_setups_endpoint_invalid_symbol():
    res = client.get("/api/strategies/momentum-setups?symbol=%20&days=1")
    assert res.status_code == 422


def test_momentum_setups_endpoint_structure(monkeypatch):
    """Endpoint returns confirmed/rejected/sim config without Alpaca."""
    import app.api.main as api_main
    from types import SimpleNamespace
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
    day = datetime(2026, 1, 15, 9, 30, tzinfo=ET)
    bars = [
        SimpleNamespace(timestamp=day + timedelta(minutes=i),
                        open=2.0, high=2.02, low=1.98, close=2.0, volume=100)
        for i in range(5)
    ]
    monkeypatch.setattr(api_main, "fetch_minute_bars",
                        lambda c, syms, days: {"AMC": bars})
    monkeypatch.setattr(api_main, "_fetch_trading_days",
                        lambda c, a, b: {day.date(): (day.time(), day.time())})
    monkeypatch.setattr(api_main, "get_data_client", lambda: object())
    monkeypatch.setattr(api_main, "get_trading_client", lambda: object())
    res = client.get("/api/strategies/momentum-setups?symbol=AMC&days=1")
    assert res.status_code == 200
    body = res.json()
    for k in ("confirmed", "rejected", "trades", "unresolved", "config",
              "session_verified", "feed", "note"):
        assert k in body
    assert body["session_verified"] is True


def test_momentum_setups_fail_closed_no_calendar(monkeypatch):
    """Unverifiable RTH calendar -> explicit unavailable, no bar analysis."""
    import app.api.main as api_main
    monkeypatch.setattr(api_main, "fetch_minute_bars",
                        lambda c, syms, days: {"AMC": []})
    monkeypatch.setattr(api_main, "_fetch_trading_days", lambda c, a, b: None)
    monkeypatch.setattr(api_main, "get_data_client", lambda: object())
    monkeypatch.setattr(api_main, "get_trading_client", lambda: object())
    res = client.get("/api/strategies/momentum-setups?symbol=AMC&days=1")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "unavailable"
    assert body["session_verified"] is False
    assert body["confirmed"] == [] and body["bars_analyzed"] == 0
    assert "fail-closed" in body["reason"]


def test_replay_sessions_and_bars():
    res = client.get("/api/research/sessions")
    assert res.status_code == 200
    for s in res.json()["sessions"]:
        assert set(s) >= {"symbol", "session", "feed", "coverage_pct"}
        assert "/" not in s["session"] and "\\" not in s["session"]
    if res.json()["sessions"]:
        s = res.json()["sessions"][0]
        r2 = client.get(f"/api/research/replay?symbol={s['symbol']}&date={s['session']}")
        assert r2.status_code == 200
        body = r2.json()
        assert body["feed"] == "sip" and body["bars"]
        assert "not live trading" in body["note"]


def test_replay_invalid():
    assert client.get("/api/research/replay?symbol=%20&date=2026-11-02").status_code == 422
    assert client.get("/api/research/replay?symbol=ZZZZZ&date=2026-11-02").status_code == 404
    assert client.get("/api/research/replay?symbol=AMC&date=bad").status_code == 422
