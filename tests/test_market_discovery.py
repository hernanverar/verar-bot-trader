from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError

from app.market_data import DEFAULT_SYMBOLS
from app.market_discovery import discover_momentum_universe

NOW = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)


def api_error(status):
    return APIError(
        '{"code": %d, "message": "err"}' % status,
        http_error=SimpleNamespace(response=SimpleNamespace(status_code=status)),
    )


def mover(symbol, pct):
    return SimpleNamespace(symbol=symbol, percent_change=pct, change=1.0, price=50.0)


def active(symbol):
    return SimpleNamespace(symbol=symbol, volume=1e6, trade_count=500)


class FakeScreener:
    def __init__(self, movers=None, actives=None, movers_error=None, actives_error=None):
        self._movers = movers
        self._actives = actives
        self._movers_error = movers_error
        self._actives_error = actives_error
        self.mover_calls = 0

    def get_market_movers(self, request_params):
        self.mover_calls += 1
        if self._movers_error:
            raise self._movers_error
        return self._movers

    def get_most_actives(self, request_params):
        if self._actives_error:
            raise self._actives_error
        return self._actives


def movers_result(symbols):
    return SimpleNamespace(
        gainers=[mover(s, 5.0) for s in symbols],
        losers=[],
        market_type="stocks",
        last_updated=NOW,
    )


def actives_result(symbols):
    return SimpleNamespace(
        most_actives=[active(s) for s in symbols],
        last_updated=NOW,
    )


def test_movers_and_actives_discovery():
    client = FakeScreener(
        movers=movers_result(["AAA", "BBB"]),
        actives=actives_result(["CCC"]),
    )
    result = discover_momentum_universe(screener_client=client, now=NOW)
    assert result.symbols == ("AAA", "BBB", "CCC")
    assert result.source == "screener:movers+actives"
    assert result.is_fallback is False
    assert result.warning is None
    assert result.as_of == NOW


def test_deduplicates_symbols():
    client = FakeScreener(
        movers=movers_result(["AAA", "BBB"]),
        actives=actives_result(["BBB", "AAA", "CCC"]),
    )
    result = discover_momentum_universe(screener_client=client, now=NOW)
    assert result.symbols == ("AAA", "BBB", "CCC")


def test_max_symbols_cap():
    client = FakeScreener(movers=movers_result([f"S{i:02d}" for i in range(40)]))
    result = discover_momentum_universe(screener_client=client, max_symbols=10, now=NOW)
    assert len(result.symbols) == 10


def test_permission_denied_falls_back_explicitly():
    client = FakeScreener(movers_error=api_error(403))
    result = discover_momentum_universe(screener_client=client, now=NOW)
    assert result.is_fallback is True
    assert result.source == "fallback_curated"
    assert result.symbols == tuple(DEFAULT_SYMBOLS)
    assert "NOT market-wide" in result.warning


def test_actives_failure_still_uses_movers():
    client = FakeScreener(
        movers=movers_result(["AAA"]),
        actives_error=api_error(500),
    )
    result = discover_momentum_universe(screener_client=client, now=NOW)
    assert result.symbols == ("AAA",)
    assert result.source == "screener:movers"
    assert result.is_fallback is False


def test_empty_screener_falls_back():
    client = FakeScreener(
        movers=movers_result([]),
        actives=actives_result([]),
    )
    result = discover_momentum_universe(screener_client=client, now=NOW)
    assert result.is_fallback is True


def test_invalid_symbols_dropped():
    client = FakeScreener(movers=movers_result(["AAA", "BAD$$$", "X" * 20, "BRK.B"]))
    result = discover_momentum_universe(
        screener_client=client, include_actives=False, now=NOW
    )
    assert result.symbols == ("AAA", "BRK.B")


def test_nontradable_assets_filtered_when_client_supplied():
    class FakeTrading:
        def get_asset(self, symbol):
            if symbol == "GONE":
                raise api_error(404)
            if symbol == "UNTRADABLE":
                return SimpleNamespace(tradable=False)
            return SimpleNamespace(tradable=True)

    client = FakeScreener(movers=movers_result(["AAA", "GONE", "UNTRADABLE", "BBB"]))
    result = discover_momentum_universe(
        screener_client=client,
        trading_client=FakeTrading(),
        include_actives=False,
        now=NOW,
    )
    assert result.symbols == ("AAA", "BBB")


def test_retry_once_on_transient_error():
    class FlakyScreener(FakeScreener):
        def get_market_movers(self, request_params):
            self.mover_calls += 1
            if self.mover_calls == 1:
                raise api_error(429)
            return self._movers

    client = FlakyScreener(movers=movers_result(["AAA"]))
    result = discover_momentum_universe(
        screener_client=client, include_actives=False, now=NOW
    )
    assert result.is_fallback is False
    assert client.mover_calls == 2


def test_persistent_rate_limit_falls_back_after_bounded_attempts():
    client = FakeScreener(movers_error=api_error(429))
    result = discover_momentum_universe(screener_client=client, now=NOW)
    assert result.is_fallback is True
    assert client.mover_calls == 2  # bounded: one retry, then stop
