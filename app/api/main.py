from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from alpaca.common.exceptions import APIError
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from app.backtest import fetch_history, run_backtest
from app.market_data import DEFAULT_SYMBOLS
from app.market_discovery import discover_momentum_universe, get_trading_client
from app.market_session import get_market_session
from app.intraday_data import completed_minute_bars, fetch_minute_bars, filter_regular_session_bars
from app.market_data import get_data_client
from app.market_session import _fetch_trading_days
from app.strategies.pullback_detector import PullbackConfig, detect_pullbacks
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
    excluded_instruments_count = 0

    try:
        trading_client = get_trading_client()
    except RuntimeError:
        trading_client = None

    if discovery_mode == "auto":
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
        excluded_instruments_count = discovery.excluded_instruments_count
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

    session_info = get_market_session(trading_client=trading_client)

    try:
        results = (
            scan_hot_stocks(symbol_list, config=config, session_info=session_info)
            if symbol_list
            else []
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except APIError as exc:
        raise HTTPException(status_code=502, detail="Upstream market-data error") from exc

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "market_session": session_info.session,
        "market_timezone": session_info.timezone,
        "market_is_open": session_info.is_open,
        "calendar_verified": session_info.calendar_verified,
        "session_warning": (
            None
            if session_info.calendar_verified
            else "Market calendar unavailable — session times are heuristic; "
                 "live eligibility disabled."
        ),
        "last_regular_session": (
            session_info.last_regular_session.isoformat()
            if session_info.last_regular_session
            else None
        ),
        "discovery_mode": discovery_mode,
        "discovery_source": discovery_source,
        "discovery_as_of": discovery_as_of,
        "discovery_warning": discovery_warning,
        "is_fallback": is_fallback,
        "excluded_instruments_count": excluded_instruments_count,
        "universe": symbol_list,
        "total_scanned": len(results),
        "eligible_count": sum(r.classification == "eligible" for r in results),
        "research_candidates_count": sum(
            r.classification == "research" for r in results
        ),
        "results": [asdict(r) for r in results[:top]],
    }


@app.get("/api/strategies/pullbacks")
def pullback_scan(
    symbol: str = Query(..., min_length=1, max_length=10),
    min_impulse_pct: float = Query(default=5.0, ge=0.5, le=50),
    impulse_min_bars: int = Query(default=5, ge=1, le=60),
    impulse_max_bars: int = Query(default=30, ge=2, le=240),
    pullback_min_bars: int = Query(default=2, ge=1, le=60),
    pullback_max_bars: int = Query(default=15, ge=1, le=120),
    retrace_min: float = Query(default=0.20, ge=0.0, le=0.95),
    retrace_max: float = Query(default=0.60, ge=0.05, le=1.0),
    days: int = Query(default=5, ge=1, le=15),
):
    """Detect pullback patterns in recent completed 1-minute bars.

    Research only — returns pattern observations, never trade instructions.
    """
    symbol = symbol.strip().upper()
    if not symbol.replace(".", "").isalnum():
        raise HTTPException(status_code=422, detail="Invalid symbol")
    if retrace_min >= retrace_max:
        raise HTTPException(status_code=422, detail="retrace_min must be below retrace_max")
    if impulse_min_bars > impulse_max_bars or pullback_min_bars > pullback_max_bars:
        raise HTTPException(status_code=422, detail="min bars must not exceed max bars")

    config = PullbackConfig(
        min_impulse_pct=min_impulse_pct,
        impulse_min_bars=impulse_min_bars,
        impulse_max_bars=impulse_max_bars,
        pullback_min_bars=pullback_min_bars,
        pullback_max_bars=pullback_max_bars,
        retrace_min=retrace_min,
        retrace_max=retrace_max,
    )

    try:
        client = get_data_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    session_verified = True
    session_warning = None
    try:
        raw = fetch_minute_bars(client, [symbol], days=days)
        bars = completed_minute_bars(raw.get(symbol, []))
    except APIError as exc:
        raise HTTPException(status_code=502, detail="Upstream market-data error") from exc

    # Regular-session prep: drop pre/post-market bars using the exchange
    # calendar. If the calendar is unavailable we cannot verify which bars
    # are in-session — fail conservatively (empty set + warning).
    try:
        trading_client = get_trading_client()
    except RuntimeError:
        trading_client = None
    trading_days = _fetch_trading_days(
        trading_client,
        (datetime.now(timezone.utc) - timedelta(days=days + 5)).date(),
        datetime.now(timezone.utc).date(),
    )
    if trading_days is None:
        session_verified = False
        session_warning = (
            "Market calendar unavailable — extended-hours bars cannot be "
            "excluded reliably; no bars analyzed."
        )
        bars = []
    else:
        bars = filter_regular_session_bars(bars, trading_days)

    try:
        patterns = detect_pullbacks(bars, config=config, symbol=symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "symbol": symbol,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "session_verified": session_verified,
        "session_warning": session_warning,
        "bars_analyzed": len(bars),
        "patterns_found": len(patterns),
        "patterns": [asdict(p) for p in patterns],
    }