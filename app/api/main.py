from dataclasses import asdict
from datetime import datetime, timezone

from alpaca.common.exceptions import APIError
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from app.backtest import fetch_history, run_backtest
from app.market_data import DEFAULT_SYMBOLS
from app.market_discovery import discover_momentum_universe, get_trading_client
from app.strategies.hot_stock_scanner import ScannerConfig, scan_hot_stocks

app = FastAPI(
    title="VERAR BOT TRADER API",
    description="Trading research and paper trading API",
    version="0.1.0",
)


class BacktestRequest(BaseModel):
    initial_cash: float = Field(default=1000.0, gt=0, le=1_000_000)
    hold_sessions: int = Field(default=5, ge=1, le=100)
    top_n: int = Field(default=3, ge=1, le=20)


@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "service": "verar-bot-trader",
        "version": "0.1.0",
        "trading_mode": "paper",
    }


@app.post("/api/backtest")
def execute_backtest(request: BacktestRequest):
    """Run a read-only historical simulation. Never place broker orders."""
    try:
        symbols = list(dict.fromkeys([*DEFAULT_SYMBOLS, "SPY"]))
        history = fetch_history(symbols)
        result = run_backtest(
            history,
            initial_cash=request.initial_cash,
            hold_sessions=request.hold_sessions,
            top_n=request.top_n,
        )
        return asdict(result)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/scanner/hot-stocks")
def hot_stocks(
    mode: str = Query(default="auto", pattern="^(auto|manual)$"),
    symbols: str | None = None,
    top: int = Query(default=20, ge=1, le=100),
    min_rvol: float | None = Query(default=None, ge=0),
    min_change_pct: float | None = Query(default=None),
):
    """Rank intraday hot stocks. Read-only market data; never places orders.

    mode=auto discovers the universe via Alpaca's screener (with an
    explicitly-flagged curated fallback). mode=manual or an explicit
    `symbols` list preserves the original scanner behavior.
    """
    discovery_mode = "manual" if symbols else mode
    discovery_source = "manual"
    discovery_as_of = None
    discovery_warning = None
    is_fallback = False

    if discovery_mode == "auto":
        try:
            trading_client = get_trading_client()
        except RuntimeError:
            trading_client = None
        try:
            discovery = discover_momentum_universe(trading_client=trading_client)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        symbol_list = list(discovery.symbols)
        discovery_source = discovery.source
        discovery_as_of = (
            discovery.as_of.isoformat() if discovery.as_of else None
        )
        discovery_warning = discovery.warning
        is_fallback = discovery.is_fallback
        if not symbol_list:
            discovery_warning = (
                discovery_warning or "Discovery returned no usable symbols"
            )
    else:
        # Strict validation applies only to client-supplied input.
        # Auto-discovered symbols are validated by the discovery service
        # and legitimately include formats like "BRK.B".
        symbol_list = (
            [s.strip().upper() for s in symbols.split(",") if s.strip()]
            if symbols
            else list(DEFAULT_SYMBOLS)
        )
        if not symbol_list or len(symbol_list) > 50 or any(
            not s.isalnum() or len(s) > 10 for s in symbol_list
        ):
            raise HTTPException(status_code=422, detail="Invalid symbols list")

    overrides = {}
    if min_rvol is not None:
        overrides["min_rvol"] = min_rvol
    if min_change_pct is not None:
        overrides["min_positive_change_pct"] = min_change_pct
    config = ScannerConfig(**overrides)

    try:
        results = scan_hot_stocks(symbol_list, config=config) if symbol_list else []
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except APIError as exc:
        raise HTTPException(status_code=502, detail="Upstream market-data error") from exc

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "discovery_mode": discovery_mode,
        "discovery_source": discovery_source,
        "discovery_as_of": discovery_as_of,
        "discovery_warning": discovery_warning,
        "is_fallback": is_fallback,
        "universe": symbol_list,
        "total_scanned": len(results),
        "eligible_count": sum(r.classification == "eligible" for r in results),
        "results": [asdict(r) for r in results[:top]],
    }