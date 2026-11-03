from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from alpaca.common.exceptions import APIError
import re
import threading
import time
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
from app.research_snapshots import load_snapshot, list_versions, snapshot_hash, ROOT as RESEARCH_ROOT
from pathlib import Path
import json as _json
import os
from app.strategies.momentum_detector import MomentumSetupConfig, detect_momentum_setups
from app.strategies.momentum_simulator import lifecycle_status, simulate_momentum_setups
from app import alert_store
from app.diagnostics import record as _diag, session_report as _session_report
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
        _diag("alpaca_error", context="scanner", error=str(exc)[:200])
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except APIError as exc:
        _diag("alpaca_error", context="scanner", error="upstream APIError")
        raise HTTPException(status_code=502, detail="Upstream market-data error") from exc

    counts: dict[str, int] = {}
    for st in results:
        counts[st.classification] = counts.get(st.classification, 0) + 1
    _diag("scanner_eval", symbols=len(results),
          eligible=counts.get("eligible", 0), research=counts.get("research", 0),
          filtered=counts.get("filtered", 0), unavailable=counts.get("unavailable", 0),
          session=session_info.session,
          calendar_verified=session_info.calendar_verified,
          duration_ms=None)

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
        _diag("alpaca_error", context="chart_bars", symbol=symbol, error=str(exc)[:200])
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    t0 = time.monotonic()
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
        _diag("alpaca_error", context="chart_bars", symbol=symbol, error="upstream APIError")
        _diag("chart_refresh", symbol=symbol, interval_min=interval, ok=False,
              fetch_ms=round((time.monotonic() - t0) * 1000, 1),
              error="Upstream market-data error", session=None)
        raise HTTPException(status_code=502, detail="Upstream market-data error") from exc
    fetch_ms = round((time.monotonic() - t0) * 1000, 1)

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

    last_ts = complete[-1].timestamp if complete else None
    _diag(
        "chart_refresh", symbol=symbol, interval_min=interval, ok=True,
        fetch_ms=fetch_ms, bars=len(bars),
        last_data_ts=int(last_ts.timestamp()) if last_ts else None,
        # bar-completion -> receipt, accounting for the bar's own duration
        data_age_ms=round((now - (last_ts + step)).total_seconds() * 1000, 1) if last_ts else None,
        session=session_info.session,
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
        _diag("alpaca_error", context="pullback_simulation", symbol=symbol,
              error="upstream APIError")
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
    t0 = time.monotonic()
    result = simulate_pullback_trades(bars, patterns, config, session_times=trading_days)
    _diag("sim_run", symbol=symbol,
          patterns_confirmed=result.patterns_confirmed,
          trades_entered=len(result.trades),
          unresolved=len(result.unresolved),
          net_pnl=result.net_pnl, exit_reasons=result.exit_reasons,
          duration_ms=round((time.monotonic() - t0) * 1000, 1))

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
        _diag("alert_eval", alerts_evaluated=0, busy=True, skipped_symbols=[],
              monitored_symbols=0, new_events=0, duration_ms=None)
        return {"new_events": [], "alerts_evaluated": 0, "busy": True}
    try:
        return _evaluate_alerts_locked()
    except Exception as exc:
        _diag("api_error", context="alerts_evaluate",
              error=f"{type(exc).__name__}: {str(exc)[:200]}")
        raise
    finally:
        _eval_lock.release()


def _evaluate_alerts_locked():
    """Evaluate all enabled alerts against server-verified Alpaca bars.

    Called by the terminal's polling loop — nothing evaluates while the
    terminal is closed. Browser-submitted prices are never trusted; bars
    are fetched server-side per monitored symbol (dedup by timestamp).
    Symbols beyond MAX_MONITORED_SYMBOLS are skipped and reported.
    """
    t_eval = time.monotonic()
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
            for e in evs:
                _diag("alert_triggered", alert_id=a.id, symbol=a.symbol,
                      alert_type=a.alert_type, bar_ts=e.ts.isoformat(),
                      eval_ts=now.isoformat(),
                      alert_latency_ms=round((now - e.ts).total_seconds() * 1000, 1),
                      trigger_price=e.trigger_price,
                      replay=e.ts < a.created_ts)  # pre-creation bar = historical
            new_events.extend(e.to_dict() for e in evs)

    def _mut(store_alerts, events):
        by_id = {a.id: a for a in alerts}
        for i, a in enumerate(store_alerts):
            if a.id in by_id:
                store_alerts[i] = by_id[a.id]
        _diag("alert_eval", alerts_evaluated=len(alerts),
              monitored_symbols=len(symbols), skipped_symbols=skipped_symbols,
              new_events=len(new_events), busy=False,
              duration_ms=round((time.monotonic() - t_eval) * 1000, 1))
        return {
            "new_events": new_events,
            "alerts_evaluated": len(alerts),
            "monitored_symbols": len(symbols),
            "skipped_symbols": skipped_symbols,
        }, store_alerts, events + [AlertEvent.from_dict(e) for e in new_events]

    return alert_store.mutate(_mut)


# ---- Phase 2D: session diagnostics (local-only) -------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@app.get("/api/diagnostics/session")
def diagnostics_session(date: str = Query(...)):
    """Aggregated local validation report for one ET session date."""
    if not _DATE_RE.match(date):
        raise HTTPException(422, "date must be YYYY-MM-DD")
    day = datetime.strptime(date, "%Y-%m-%d").date()
    try:
        trading_client = get_trading_client()
        cal = _fetch_trading_days(trading_client, day, day)
    except Exception:
        cal = None
    return _session_report(day, (cal or {}).get(day))


@app.post("/api/diagnostics/client")
def diagnostics_client(body: dict = Body(...)):
    """Record a browser-reported event (e.g., notification attempt).

    Client-submitted data is labeled browser-reported and never treated
    as server-verified. Schema-constrained; timestamps are validated.
    """
    kind = body.get("kind")
    valid = {"notification_attempt", "notification_failed",
             "notification_denied", "notifications_unsupported"}
    if kind not in valid:
        raise HTTPException(422, "unsupported client event kind")
    reported = body.get("reported_at")
    if reported is not None:
        try:
            datetime.fromisoformat(str(reported))
        except ValueError:
            raise HTTPException(422, "invalid reported_at")
    _diag("client_event", client_kind=kind,
          event_id=body.get("event_id"), symbol=body.get("symbol"),
          reported_at=reported, detail=str(body.get("detail", ""))[:300])
    return {"recorded": True, "verified": "browser-reported"}


# ---- Phase 3A: momentum setups (read-only research) ----------------------

@app.get("/api/strategies/momentum-setups")
def momentum_setups(
    symbol: str = Query(..., min_length=1, max_length=10),
    days: int = Query(default=5, ge=1, le=30),
    target_r_multiple: float = Query(default=2.0, ge=0.5, le=10.0),
):
    """Detected Ross-style momentum setups + simulated outcomes.

    Research only — completed RTH 1m bars, no orders, no live signals.
    Historical scanner eligibility is NOT claimed unless reconstructable.
    """
    symbol = symbol.strip().upper()
    if not symbol.replace(".", "").isalnum():
        raise HTTPException(status_code=422, detail="Invalid symbol")

    feed = _data_feed()
    try:
        data_client = get_data_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        raw = fetch_minute_bars(data_client, [symbol], days=days).get(symbol, [])
    except APIError as exc:
        _diag("alpaca_error", context="momentum_setups", symbol=symbol,
              error="upstream APIError")
        raise HTTPException(status_code=502, detail="Upstream market-data error") from exc

    trading_client = None
    try:
        trading_client = get_trading_client()
    except RuntimeError:
        pass
    now = datetime.now(timezone.utc)
    trading_days = _fetch_trading_days(
        trading_client, (now - timedelta(days=days + 5)).date(), now.date()
    )
    session_verified = trading_days is not None
    if not session_verified:
        # Fail-closed: never analyze extended-hours/unverified bars.
        return {
            "symbol": symbol,
            "as_of": now.isoformat(),
            "status": "unavailable",
            "reason": "RTH session calendar could not be verified — "
                      "setups suppressed (fail-closed).",
            "feed": feed.value if hasattr(feed, "value") else str(feed),
            "session_verified": False,
            "confirmed": [], "rejected": [], "trades": [],
            "unresolved": [], "skipped": [],
            "bars_analyzed": 0,
            "note": "historical research — not live trade signals",
        }
    bars = filter_regular_session_bars(
        completed_minute_bars(raw, now=now), trading_days
    )

    t0 = time.monotonic()
    setups, rejected = detect_momentum_setups(bars, symbol=symbol)
    sim = simulate_momentum_setups(
        bars, setups,
        TradeConfig(risk_reward=target_r_multiple),
        session_times=trading_days,
    )
    _diag("sim_run", symbol=symbol, patterns_confirmed=len(setups),
          trades_entered=len(sim.trades), unresolved=len(sim.unresolved),
          net_pnl=sim.net_pnl, exit_reasons=sim.exit_reasons,
          duration_ms=round((time.monotonic() - t0) * 1000, 1))

    def _setup_id(s):
        return f"{symbol}-{int(s.confirm_ts.timestamp())}"

    return {
        "symbol": symbol,
        "as_of": now.isoformat(),
        "feed": feed.value if hasattr(feed, "value") else str(feed),
        "session_verified": session_verified,
        "bars_analyzed": len(bars),
        "note": "historical research — not live trade signals",
        "config": {
            "impulse_min_pct": MomentumSetupConfig().impulse_min_pct,
            "pullback_max_retrace": MomentumSetupConfig().pullback_max_retrace,
            "breakout_conf_vol_x": MomentumSetupConfig().breakout_conf_vol_x,
            "target_r_multiple": target_r_multiple,
        },
        "status": "ok",
        "confirmed": [
            {
                "id": _setup_id(s),
                "lifecycle": lifecycle_status(s.confirm_ts, sim),
                "confirm_ts": s.confirm_ts.isoformat(),
                "confirm_price": s.confirm_price,
                "impulse_high": s.impulse_high,
                "anchor_low": s.anchor_low,
                "pullback_low": s.pullback_low,
                "pullback_bars": s.pullback_bars,
                "stop_price": s.stop_price,
                "pending_entry": s.pending_entry,
            }
            for s in setups
        ],
        "rejected": [
            {"ts": r.ts.isoformat(), "reason": r.reason} for r in rejected
        ],
        "trades": [asdict(t) for t in sim.trades],
        "unresolved": [asdict(t) for t in sim.unresolved],
        "skipped": [[ts.isoformat(), r] for ts, r in sim.skipped],
        "win_rate": sim.win_rate,
        "net_pnl": sim.net_pnl,
        "ending_equity": sim.ending_equity,
        "exit_reasons": sim.exit_reasons,
    }


# ---- Phase 4A: market replay (read-only, local snapshots) ----------------

@app.get("/api/research/sessions")
def replay_sessions():
    """List available historical SIP snapshots — no filesystem paths exposed."""
    base = RESEARCH_ROOT / "snapshots"
    sessions = []
    seen: set[tuple[str, str]] = set()
    if not base.is_dir():
        return {"sessions": []}
    for sym_dir in sorted(base.iterdir()):
        if not sym_dir.is_dir():
            continue
        for sess_dir in sorted(sym_dir.iterdir()):
            sip_dir = sess_dir / "sip"
            d = sip_dir if sip_dir.is_dir() else None
            if d is None:
                continue
            # one row per session — latest version wins (unique key)
            metas = sorted(
                (p for p in d.glob("meta_v*.json")
                 if p.stem[6:].isdigit()),
                key=lambda p: int(p.stem[6:]))
            if not metas or (sym_dir.name, sess_dir.name) in seen:
                continue
            try:
                meta = _json.loads(metas[-1].read_text())
            except Exception:
                continue
            seen.add((sym_dir.name, sess_dir.name))
            sessions.append({
                "symbol": sym_dir.name,
                "session": sess_dir.name,
                "feed": meta.get("feed", "sip"),
                "version": meta.get("version"),
                "coverage_pct": meta.get("coverage_pct"),
                "confidence": meta.get("confidence"),
                "bars": meta.get("bars"),
            })
    return {"sessions": sessions}


@app.get("/api/research/replay")
def replay_bars(symbol: str = Query(..., min_length=1, max_length=10),
                date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$")):
    """Return the immutable SIP snapshot for replay — normalized OHLCV bars."""
    symbol = symbol.strip().upper()
    if not symbol.replace(".", "").isalnum():
        raise HTTPException(status_code=422, detail="Invalid symbol")
    try:
        session_date = datetime.fromisoformat(date).date()
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date") from None
    recs = load_snapshot(symbol, date, feed="sip")
    if not recs:
        raise HTTPException(
            status_code=404,
            detail="Snapshot unavailable for symbol/date")
    vs = list_versions(symbol, date, feed="sip")
    sha = snapshot_hash(symbol, date, vs[-1], feed="sip") if vs else None
    meta_path = (RESEARCH_ROOT / "snapshots" / symbol / date / "sip"
                 / f"meta_v{vs[-1]}.json" if vs else None)
    meta = {}
    if meta_path and meta_path.exists():
        try:
            meta = _json.loads(meta_path.read_text())
        except Exception:
            meta = {}
    bars = [{"t": r["t"], "o": r["o"], "h": r["h"], "l": r["l"],
             "c": r["c"], "v": r["v"]} for r in recs]
    return {
        "symbol": symbol,
        "session": date,
        "feed": "sip",
        "timezone": "America/New_York",
        "snapshot_sha256": sha,
        "coverage_pct": meta.get("coverage_pct"),
        "confidence": meta.get("confidence"),
        "note": "historical replay — not live trading",
        "bars": bars,
    }


# ---- Phase 4B: premarket scanner (free IEX, research only) ----------------

@app.get("/api/scanner/premarket")
def premarket_scanner(
    symbols: str | None = Query(default=None),
    use_screener: bool = Query(default=True),
    scan: bool = Query(default=False),
    discover_gap: float = Query(default=5.0),
    scan_max: int = Query(default=25, ge=1, le=100),
    baseline_days: int = Query(default=5, ge=1, le=15),
):
    """Premarket momentum monitor — research data, free IEX only.
    Volume is partial IEX, never consolidated."""
    from app.premarket_scanner import (
        PremarketCfg, discover_symbols, symbol_metrics,
        elapsed_window, pm_bounds, prev_rth_close,
        baseline_volumes, gap_scan, load_universe_symbols,
        MIN_BASELINE_VOLUME)
    import requests as _req
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    now = datetime.now(ET)
    today = now.date()
    cfg = PremarketCfg(baseline_days=baseline_days)
    try:
        dc = get_data_client()
        tc = get_trading_client()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"data client unavailable: {exc}") from exc

    trading_days = _fetch_trading_days(
        tc, today - timedelta(days=baseline_days * 3), today)
    sessions = sorted(trading_days.keys()) if trading_days else []
    in_pm = pm_bounds(today)[0] <= now < pm_bounds(today)[1]
    pm_done = now >= pm_bounds(today)[1]
    if today not in (sessions or [today]) or (not in_pm and not pm_done):
        return {"market": "premarket_not_active", "note":
                "outside 04:00-09:29 ET or non-trading day", "rows": [],
                "feed": "iex", "checked_at": now.isoformat()}

    watch = [s.strip().upper() for s in symbols.split(",")] if symbols else []
    rows, disc_err = discover_symbols(
        (lambda: _most_actives()) if use_screener else (lambda: []),
        watch)
    scan_stats = {"universe": 0, "scanned": 0, "candidates": 0,
                  "requests": 0, "cached": False, "cache_age_s": 0.0}
    if scan:
        from app.premarket_scanner import cached_gap_scan

        def _do_scan():
            st = {"universe": 0, "scanned": 0, "candidates": 0,
                  "requests": 0, "errors": []}
            uni = load_universe_symbols()
            st["universe"] = len(uni)
            snaps = {}
            import time as _t
            for chunk in [uni[i:i+400] for i in range(0, len(uni), 400)]:
                for attempt in range(2):
                    try:
                        rr = _req.get(
                            "https://data.alpaca.markets/v2/stocks/snapshots",
                            params={"symbols": ",".join(chunk), "feed": "iex"},
                            headers={"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY"),
                                     "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY")},
                            timeout=30)
                        st["requests"] += 1
                        if rr.ok:
                            snaps.update(rr.json())
                            break
                        if rr.status_code == 429 and attempt == 0:
                            _t.sleep(2)
                            continue
                        st["errors"].append(f"batch {rr.status_code}")
                        break
                    except _req.RequestException as exc:
                        if attempt == 0:
                            _t.sleep(2)
                            continue
                        st["requests"] += 1
                        st["errors"].append(str(exc)[:80])
                        break
            st["scanned"] = len(snaps)
            found = gap_scan(snaps, cfg, discover_gap_pct=discover_gap,
                             window=pm_bounds(today))
            st["candidates"] = len(found)
            return {"stats": st, "found": found}
        try:
            sr = cached_gap_scan(
                _do_scan,
                key=(today.isoformat(), discover_gap,
                     cfg.price_min, cfg.price_max))
            scan_stats.update(sr["result"]["stats"])
            scan_stats["cached"] = sr["cached"]
            scan_stats["cache_age_s"] = sr["cache_age_s"]
            seen = {s for s, _ in rows}
            for f_ in sr["result"]["found"][:scan_max]:
                if f_["symbol"] not in seen:
                    rows.append((f_["symbol"], "gap_scan"))
                    seen.add(f_["symbol"])
        except Exception as exc:
            disc_err["gap_scan"] = str(exc)[:120]
    market_label = ("premarket" if in_pm
                    else "premarket_ended" if pm_done else "premarket_pending")
    results = {"market": market_label, "feed": "iex", "checked_at": now.isoformat(),
               "in_window": in_pm, "discovery_errors": disc_err,
               "scan": scan_stats,
               "notes": ["volumes/RVOL are IEX-partial, not consolidated",
                         "prev_close is IEX daily close approximation, not official",
                         "api_calls counts logical batched requests; SDK paginates internally"],
               "api_calls": {"screener": 1 if use_screener else 0,
                             "daily": 0, "minute": 0, "baseline": 0},
               "rows": []}
    if not rows:
        return results

    try:
        daily_req = StockBarsRequest(
            symbol_or_symbols=[s for s, _ in rows], timeframe=TimeFrame.Day,
            start=now - timedelta(days=baseline_days * 3 + 10), end=now,
            feed="iex")
        daily = dc.get_stock_bars(daily_req).data
        results["api_calls"]["daily"] += 1
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"daily bars failed: {exc}") from exc

    start, _ = elapsed_window(today, now)
    try:
        pm_req = StockBarsRequest(
            symbol_or_symbols=[s for s, _ in rows], timeframe=TimeFrame.Minute,
            start=start, end=now, feed="iex")
        pm = dc.get_stock_bars(pm_req).data
        results["api_calls"]["minute"] += 1
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"premarket bars failed: {exc}") from exc

    try:
        hist_req = StockBarsRequest(
            symbol_or_symbols=[s for s, _ in rows], timeframe=TimeFrame.Minute,
            start=now - timedelta(days=baseline_days * 3),
            end=pm_bounds(today)[0], feed="iex")
        hist = dc.get_stock_bars(hist_req).data
        results["api_calls"]["baseline"] += 1
    except Exception as exc:
        hist = {}
        results["discovery_errors"]["baseline"] = str(exc)[:120]

    for sym, src in rows:
        pm_bars = [b for b in pm.get(sym, [])
                   if pm_bounds(today)[0] <= b.timestamp < pm_bounds(today)[1]]
        pc, pcd = prev_rth_close(daily.get(sym, []), sessions, today)
        byday: dict[str, list] = {}
        for b in hist.get(sym, []):
            byday.setdefault(b.timestamp.date().isoformat(), []).append(b)
        bases = baseline_volumes(byday, sessions, today, now, baseline_days)
        m = symbol_metrics(pm_bars, pc, pcd, bases, now, cfg)
        results["rows"].append({"symbol": sym, "source": src, **m})
    return results


def _most_actives() -> list[dict]:
    """Raw REST screener — free tier, ~1 call."""
    import requests as _req
    key = os.getenv("ALPACA_API_KEY"); sec = os.getenv("ALPACA_SECRET_KEY")
    if not key or not sec:
        return []
    r = _req.get(
        "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives",
        params={"by": "volume", "top": 25},
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec},
        timeout=15)
    r.raise_for_status()
    return r.json().get("most_actives", [])
