from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from alpaca.common.exceptions import APIError
import re
import threading
from datetime import time as dt_time
from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from app.backtest import fetch_history, run_backtest
from app.market_data import DEFAULT_SYMBOLS
from app.market_discovery import discover_momentum_universe, get_trading_client
from app.market_session import get_market_session
from app.intraday_data import completed_minute_bars, fetch_minute_bars, filter_regular_session_bars
from app.market_data import MARKET_TZ, _data_feed, get_data_client
from app.market_session import _fetch_trading_days
from app.strategies.pullback_detector import PullbackConfig, detect_pullbacks
from app.strategies.trade_simulator import TradeConfig, simulate_pullback_trades
from app.chart_data import rth_vwap, session_vwap
from app import alert_store
from app.alerts import AlertEvent, VALID_TYPES, evaluate_alert, new_alert, rearm
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
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


@app.get("/api/charts/bars")
def chart_bars(
    symbol: str = Query(..., min_length=1, max_length=10),
    interval: int = Query(default=1, ge=1, le=60),
    days: int = Query(default=5, ge=1, le=30),
):
    """OHLCV bars for the terminal chart. Read-only market data.

    IEX feed caveat: not consolidated tape — volume understates the full
    market. Response carries the feed label so the UI can disclose it.
    """
    symbol = symbol.strip().upper()
    if not symbol.replace(".", "").isalnum():
        raise HTTPException(status_code=422, detail="Invalid symbol")
    if interval not in (1, 5, 15):
        raise HTTPException(status_code=422, detail="interval must be 1, 5 or 15 minutes")

    feed = _data_feed()
    try:
        client = get_data_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        result = client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(interval, TimeFrameUnit.Minute),
                start=datetime.now(timezone.utc) - timedelta(days=days + 3),
                feed=feed,
                sort="asc",
            )
        )
        raw = result.data.get(symbol, [])
    except APIError as exc:
        raise HTTPException(status_code=502, detail="Upstream market-data error") from exc

    # Completed bars only — never render an unfinished candle.
    now = datetime.now(timezone.utc)
    step = timedelta(minutes=interval)
    complete = [b for b in raw if b.timestamp + step <= now]
    bars = [
        {
            "time": int(b.timestamp.timestamp()),
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in complete
    ]

    try:
        trading_client = get_trading_client()
    except RuntimeError:
        trading_client = None
    session_info = get_market_session(now=now, trading_client=trading_client)
    cal = _fetch_trading_days(
        trading_client,
        (now - timedelta(days=days + 5)).date(),
        now.date(),
    )

    return {
        "symbol": symbol,
        "interval_min": interval,
        "feed": feed.value if hasattr(feed, "value") else str(feed),
        "as_of": now.isoformat(),
        "market_session": session_info.session,
        "market_is_open": session_info.is_open,
        "last_data_ts": int(complete[-1].timestamp.timestamp()) if complete else None,
        "vwap_verified": cal is not None,
        "rth_vwap": [
            {"time": int(ts.timestamp()), "value": round(v, 4)}
            for ts, v in (rth_vwap(complete, cal) if cal is not None else [])
        ],
        "vwap": [
            {"time": int(ts.timestamp()), "value": round(v, 4)}
            for ts, v in session_vwap(complete)
        ],
        "bars": bars,
    }


@app.get("/api/strategies/pullback-simulation")
def pullback_simulation(
    symbol: str = Query(..., min_length=1, max_length=10),
    profit_target_pct: float = Query(default=15.0, ge=1.0, le=100.0),
    stop_loss_pct: float = Query(default=5.0, ge=0.1, le=50.0),
    risk_pct: float = Query(default=0.01, gt=0, le=0.25),
    initial_equity: float = Query(default=2000.0, ge=100.0, le=1_000_000.0),
    days: int = Query(default=5, ge=1, le=15),
):
    """Simulate trades on confirmed pullbacks. Research only — no orders."""
    symbol = symbol.strip().upper()
    if not symbol.replace(".", "").isalnum():
        raise HTTPException(status_code=422, detail="Invalid symbol")

    try:
        client = get_data_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        raw = fetch_minute_bars(client, [symbol], days=days)
        bars = completed_minute_bars(raw.get(symbol, []))
    except APIError as exc:
        raise HTTPException(status_code=502, detail="Upstream market-data error") from exc

    try:
        trading_client = get_trading_client()
    except RuntimeError:
        trading_client = None
    trading_days = _fetch_trading_days(
        trading_client,
        (datetime.now(timezone.utc) - timedelta(days=days + 5)).date(),
        datetime.now(timezone.utc).date(),
    )
    session_verified = trading_days is not None
    if session_verified:
        bars = filter_regular_session_bars(bars, trading_days)
    else:
        bars = []

    patterns = detect_pullbacks(bars, symbol=symbol)
    config = TradeConfig(
        initial_equity=initial_equity,
        risk_pct=risk_pct,
        stop_loss_pct=stop_loss_pct,
        profit_target_pct=profit_target_pct,
    )
    result = simulate_pullback_trades(bars, patterns, config, session_times=trading_days)

    return {
        "symbol": symbol,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "session_verified": session_verified,
        "config": {
            "initial_equity": initial_equity,
            "risk_pct": risk_pct,
            "stop_loss_pct": stop_loss_pct,
            "profit_target_pct": profit_target_pct,
        },
        "bars_analyzed": len(bars),
        "patterns_confirmed": result.patterns_confirmed,
        "trades_entered": len(result.trades),
        "unresolved_count": len(result.unresolved),
        "skipped": [[ts.isoformat(), reason] for ts, reason in result.skipped],
        "win_rate": result.win_rate,
        "gross_pnl": result.gross_pnl,
        "net_pnl": result.net_pnl,
        "ending_equity": result.ending_equity,
        "max_drawdown": result.max_drawdown,
        "exit_reasons": result.exit_reasons,
        "trades": [asdict(t) for t in result.trades],
        "unresolved": [asdict(t) for t in result.unresolved],
    }

# ---- Phase 2C: configurable alerts (informational only) -----------------

ALERT_SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.]{0,9}$")
MAX_MONITORED_SYMBOLS = 10
MOMENTUM_EVAL_INTERVAL = timedelta(seconds=60)


def _validate_alert_body(body: dict, partial: bool = False) -> tuple | None:
    if not partial or "symbol" in body:
        symbol = str(body.get("symbol", "")).upper()
        if not ALERT_SYMBOL_RE.match(symbol):
            return None, "invalid symbol"
    alert_type = body.get("alert_type")
    if alert_type is not None and alert_type not in VALID_TYPES:
        return None, "invalid alert_type"
    level = body.get("level")
    if level is not None and not (isinstance(level, (int, float)) and 0 < level < 100_000):
        return None, "invalid level"
    return True, ""


@app.get("/api/alerts")
def list_alerts():
    alerts = alert_store.load_alerts()
    events = alert_store.load_events()
    return {
        "alerts": [a.to_dict() for a in alerts],
        "events": [e.to_dict() for e in events],
        "note": "completed-bar based, informational only — evaluates while terminal is open",
    }


@app.post("/api/alerts")
def create_alert(body: dict = Body(...)):
    ok, err = _validate_alert_body(body)
    if ok is None:
        raise HTTPException(422, err)
    symbol = str(body["symbol"]).upper()
    alert_type = body["alert_type"]
    level = body.get("level")
    if alert_type in ("price_above", "price_below", "stop_level") and level is None:
        raise HTTPException(422, "level required for this alert type")

    alert = new_alert(symbol, alert_type, float(level) if level is not None else None)
    # Initialize baseline from the latest server-fetched completed bar so an
    # alert created while already past the threshold cannot false-trigger.
    try:
        bars = completed_minute_bars(
            fetch_minute_bars(get_data_client(), _data_feed(), symbol, days=1)
        )
        if bars:
            alert.state["last_close"] = bars[-1].close
            alert.state["last_ts"] = bars[-1].timestamp.isoformat()
            if alert_type == "hod_breakout":
                day = bars[-1].timestamp.astimezone(MARKET_TZ).date()
                sess = _fetch_trading_days(get_trading_client(), day, day) or {}
                lo, hi = sess.get(day, (dt_time(9, 30), dt_time(16, 0)))
                rth = [
                    b for b in bars
                    if (t := b.timestamp.astimezone(MARKET_TZ)).date() == day
                    and lo <= t.time() < hi
                ]  # RTH only — premarket never seeds the baseline
                alert.state["session_day"] = day.isoformat()
                alert.state["session_high"] = max((b.high for b in rth), default=None)
    except Exception:
        pass  # baseline stays empty — first bars will seed it silently

    def _mut(alerts, events):
        if len(alerts) >= 200:
            raise HTTPException(422, "alert limit reached")
        alerts.append(alert)
        return alert.to_dict(), alerts, events

    return alert_store.mutate(_mut)


@app.patch("/api/alerts/{alert_id}")
def update_alert(alert_id: str, body: dict = Body(...)):
    ok, err = _validate_alert_body(body, partial=True)
    if ok is None:
        raise HTTPException(422, err)

    def _mut(alerts, events):
        for a in alerts:
            if a.id == alert_id:
                if "enabled" in body:
                    a.enabled = bool(body["enabled"])
                    if not a.enabled:
                        a.state["fired"] = False
                if "level" in body:
                    a.level = float(body["level"])
                    a.state["last_close"] = None  # re-arm from next bar
                    a.state["last_ts"] = None
                    a.state["fired"] = False
                return a.to_dict(), alerts, events
        raise HTTPException(404, "alert not found")

    return alert_store.mutate(_mut)


@app.delete("/api/alerts/{alert_id}")
def delete_alert(alert_id: str):
    def _mut(alerts, events):
        keep = [a for a in alerts if a.id != alert_id]
        if len(keep) == len(alerts):
            raise HTTPException(404, "alert not found")
        return {"deleted": alert_id}, keep, events
    return alert_store.mutate(_mut)


@app.post("/api/alerts/{alert_id}/rearm")
def rearm_alert(alert_id: str):
    def _mut(alerts, events):
        for a in alerts:
            if a.id == alert_id:
                rearm(a)
                return a.to_dict(), alerts, events
        raise HTTPException(404, "alert not found")
    return alert_store.mutate(_mut)


_eval_lock = threading.Lock()


@app.post("/api/alerts/evaluate")
def evaluate_alerts():
    if not _eval_lock.acquire(blocking=False):
        return {"new_events": [], "alerts_evaluated": 0, "busy": True}
    try:
        return _evaluate_alerts_locked()
    finally:
        _eval_lock.release()


def _evaluate_alerts_locked():
    """Evaluate all enabled alerts against server-verified Alpaca bars.

    Called by the terminal's polling loop — nothing evaluates while the
    terminal is closed. Browser-submitted prices are never trusted; bars
    are fetched server-side per monitored symbol (dedup by timestamp).
    Symbols beyond MAX_MONITORED_SYMBOLS are skipped and reported.
    """
    alerts = [a for a in alert_store.load_alerts() if a.enabled]
    all_symbols = sorted({a.symbol for a in alerts})
    symbols = all_symbols[:MAX_MONITORED_SYMBOLS]
    skipped_symbols = all_symbols[MAX_MONITORED_SYMBOLS:]
    new_events: list[dict] = []
    now = datetime.now(timezone.utc)

    for sym in symbols:
        sym_alerts = [a for a in alerts if a.symbol == sym]
        try:
            data_client = get_data_client()
            bars = completed_minute_bars(
                fetch_minute_bars(data_client, [sym], days=1).get(sym, [])
            )
        except Exception:
            continue  # data unavailable — evaluate nothing, alert nothing
        trading_client = None
        try:
            trading_client = get_trading_client()
        except RuntimeError:
            pass
        today = (now.astimezone(MARKET_TZ)).date()
        sess = _fetch_trading_days(trading_client, today - timedelta(days=3), today)

        momentum_eligible = None
        if any(a.alert_type == "momentum" for a in sym_alerts):
            last_eval = max(
                (a.state.get("momentum_eval_ts") for a in sym_alerts if a.state.get("momentum_eval_ts")),
                default=None,
            )
            if last_eval is None or now - datetime.fromisoformat(last_eval) >= MOMENTUM_EVAL_INTERVAL:
                try:
                    session_info = get_market_session(now=now, trading_client=trading_client)
                    res = scan_hot_stocks([sym], data_client, now=now, session_info=session_info)
                    momentum_eligible = bool(res and res[0].ross_eligible)
                    for a in sym_alerts:
                        a.state["momentum_eval_ts"] = now.isoformat()
                except Exception:
                    momentum_eligible = None

        for a in sym_alerts:
            new_bars = [
                b for b in bars
                if a.state.get("last_ts") is None or b.timestamp > datetime.fromisoformat(a.state["last_ts"])
            ]
            evs = evaluate_alert(a, new_bars, session_times=sess, momentum_eligible=momentum_eligible)
            if a.state.get("last_ts"):
                a.state["last_ts"] = a.state["last_ts"] if isinstance(a.state["last_ts"], str) else a.state["last_ts"].isoformat()
            if a.state.get("session_day") and not isinstance(a.state["session_day"], str):
                a.state["session_day"] = a.state["session_day"].isoformat()
            new_events.extend(e.to_dict() for e in evs)

    def _mut(store_alerts, events):
        by_id = {a.id: a for a in alerts}
        for i, a in enumerate(store_alerts):
            if a.id in by_id:
                store_alerts[i] = by_id[a.id]
        return {
            "new_events": new_events,
            "alerts_evaluated": len(alerts),
            "monitored_symbols": len(symbols),
            "skipped_symbols": skipped_symbols,
        }, store_alerts, events + [AlertEvent.from_dict(e) for e in new_events]

    return alert_store.mutate(_mut)
