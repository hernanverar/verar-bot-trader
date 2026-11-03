"""Premarket momentum scanner — free Alpaca IEX data only.

Two-stage: broad discovery (most-actives + watchlist) then focused
monitoring via batched minute bars. Metrics are labeled partial-IEX —
no prints does NOT mean no market activity. RVOL compares same elapsed
premarket window vs >=3 prior sessions; never EOD RVOL.
"""

from dataclasses import dataclass, field
from pathlib import Path
from datetime import date, datetime, timedelta, time
from statistics import median
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PM_OPEN = time(4, 0)
PM_END = time(9, 30)          # bars before 9:30 ET
MIN_BASELINE_SESSIONS = 3
MIN_BASELINE_VOLUME = 100     # near-zero denominators are unusable


@dataclass(frozen=True)
class PremarketCfg:
    price_min: float = 1.0
    price_max: float = 10.0
    gap_min_pct: float = 10.0
    baseline_days: int = 5
    stale_after_s: float = 120.0


def pm_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, PM_OPEN, tzinfo=ET)
    end = datetime.combine(day, PM_END, tzinfo=ET)
    return start, end


def elapsed_window(day: date, now_et: datetime) -> tuple[datetime, datetime]:
    """Premarket window [open, min(now, 9:30)) for `day`."""
    start, end = pm_bounds(day)
    if now_et < end:
        end = now_et
    return start, end


def filter_pm(bars: list, day: date, now_et: datetime) -> list:
    """Bars inside today's premarket window only."""
    start, end = elapsed_window(day, now_et)
    return [b for b in bars if start <= b.timestamp < end]


def premarket_rvol(pm_vol: float, baselines: list[float]) -> dict:
    """Same-window cumulative RVOL — needs >=3 valid sessions and a
    non-trivial denominator."""
    valid = [v for v in baselines if v >= MIN_BASELINE_VOLUME]
    if len(valid) < MIN_BASELINE_SESSIONS:
        return {"rvol": None, "baseline_n": len(valid),
                "status": "insufficient_baseline"}
    base = median(valid)
    if base < MIN_BASELINE_VOLUME:
        return {"rvol": None, "baseline_n": len(valid),
                "status": "near_zero_baseline"}
    return {"rvol": round(pm_vol / base, 2), "baseline_n": len(valid),
            "status": "ok"}


def symbol_metrics(
    pm_bars: list, prev_close: float | None, prev_close_date: date | None,
    baselines: list[float], now: datetime, cfg: PremarketCfg,
) -> dict:
    """Metrics for one symbol — pm_bars already filtered to today's window."""
    r: dict = {"feed": "iex", "rvol": None, "baseline_n": 0}
    if not pm_bars:
        r.update(status="no_iex_prints", last=None, gap_pct=None,
                 pm_volume=0, pm_high=None, pm_low=None,
                 observed_ts=None, age_s=None,
                 quality="no prints on IEX — not proof of no activity")
        return r
    last = max(pm_bars, key=lambda b: b.timestamp)
    r.update(last=round(last.close, 4),
             observed_ts=last.timestamp.isoformat(),
             age_s=round((now - last.timestamp).total_seconds(), 1),
             pm_volume=sum(b.volume for b in pm_bars),
             pm_high=max(b.high for b in pm_bars),
             pm_low=min(b.low for b in pm_bars),
             prev_close=prev_close,
             prev_close_date=prev_close_date.isoformat() if prev_close_date else None)
    if prev_close is None:
        r["gap_pct"] = None
        r["status"] = "missing_prev_close"
    else:
        r["gap_pct"] = round((last.close / prev_close - 1) * 100, 2)
        r["status"] = "ok" if r["age_s"] <= cfg.stale_after_s else "stale"
    rv = premarket_rvol(r["pm_volume"], baselines)
    r["rvol"], r["baseline_n"] = rv["rvol"], rv["baseline_n"]
    if r["status"] == "ok" and rv["status"] != "ok":
        r["status"] = f"ok; rvol_{rv['status']}"
    elif rv["status"] != "ok" and r["status"] not in ("no_iex_prints",):
        r["rvol_status"] = rv["status"]
    if prev_close is not None:
        r["qualified"] = (cfg.price_min <= last.close <= cfg.price_max
                          and (r["gap_pct"] or 0) >= cfg.gap_min_pct)
    else:
        r["qualified"] = False
    return r


def dedup_symbols(discovered: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(symbol, source) pairs — first source wins."""
    seen: dict[str, str] = {}
    for sym, src in discovered:
        seen.setdefault(sym.upper(), src)
    return sorted(seen.items())


# ---- live acquisition ----------------------------------------------------

def discover_symbols(tc, watchlist: list[str] | None = None,
                     top_n: int = 25) -> tuple[list[tuple[str, str]], dict]:
    """(symbol, source) pairs from most-actives + watchlist. tc must be a
    callable returning the screener list so tests can inject."""
    found: list[tuple[str, str]] = []
    errors: dict = {"screener": None}
    try:
        for row in tc()[:top_n]:
            sym = row.get("symbol")
            if sym:
                found.append((sym, "most_actives"))
    except Exception as exc:
        errors["screener"] = str(exc)[:120]
    for sym in watchlist or []:
        found.append((sym, "watchlist"))
    return dedup_symbols(found), errors


def prev_rth_close(daily: list, sessions: list[date], today: date) -> tuple[float | None, date | None]:
    """Close of the most recent completed RTH session strictly before today."""
    prior = {b.timestamp.date(): b for b in daily if b.timestamp.date() < today}
    for d in sorted(prior.keys(), reverse=True):
        if d in sessions:
            return prior[d].close, d
    return None, None


def baseline_volumes(daily_pm: dict[str, list], sessions: list[date],
                     today: date, now_et: datetime,
                     baseline_days: int) -> list[float]:
    """Same-elapsed-window PM volume per prior session for ONE symbol."""
    _, end = elapsed_window(today, now_et)
    elapsed = (end - pm_bounds(today)[0]).total_seconds()
    vols = []
    prior = sorted(d for d in sessions if d < today)[-baseline_days:]
    for d in prior:
        s = pm_bounds(d)[0]
        e = s + timedelta(seconds=elapsed)
        v = sum(b.volume for b in daily_pm.get(d.isoformat(), [])
                if s <= b.timestamp < e)
        if v:
            vols.append(v)
    return vols


def gap_scan(snaps: dict, cfg: PremarketCfg,
             discover_gap_pct: float = 5.0,
             window: tuple[datetime, datetime] | None = None) -> list[dict]:
    """Stage-1 discovery from bulk snapshots (latestTrade + prevDailyBar).
    Cheap: price+gap per symbol with zero extra requests.
    `window` (tz-aware [start, end)): only trades inside the actual
    premarket interval count — regular-session/postmarket trades never
    manufacture a premarket gap."""
    out = []
    for sym, s in snaps.items():
        lt, pdb = (s or {}).get("latestTrade") or {}, (s or {}).get("prevDailyBar") or {}
        px, pc, ts = lt.get("p"), pdb.get("c"), lt.get("t")
        if not px or not pc or not ts:
            continue
        if window is not None:
            try:
                tt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET)
                if not (window[0] <= tt < window[1]):
                    continue
            except (ValueError, TypeError):
                continue
        gap = (px / pc - 1) * 100
        if cfg.price_min <= px <= cfg.price_max and gap >= discover_gap_pct:
            out.append({"symbol": sym, "source": "gap_scan", "price": px,
                        "gap_pct": round(gap, 2), "trade_ts": ts})
    return sorted(out, key=lambda r: -r["gap_pct"])


def load_universe_symbols() -> list[str]:
    """Small-cap-eligible symbols from the newest saved universe manifest."""
    import json
    d = Path(__file__).resolve().parent.parent / "data" / "research" / "universe"
    files = sorted(d.glob("universe_*.json")) if d.is_dir() else []
    if not files:
        return []
    return json.loads(files[-1].read_text())["symbols"]


# ---- broad-scan cache + concurrency guard --------------------------------
import threading
_SCAN_TTL = 240.0          # 4 min — broad discovery cadence
_scan_state = {"ts": None, "result": None, "key": None}
_scan_lock = threading.Lock()
_scan_inflight = threading.Event()
_scan_inflight.set()


def cached_gap_scan(fetch, key=None, now_fn=None,
                    ttl: float = _SCAN_TTL) -> dict:
    """Run `fetch()` at most once per `ttl` per `key` (session date +
    discovery params). A key change invalidates the cache immediately —
    yesterday's results are never served for today.
    Concurrent callers share the in-flight scan."""
    import time as _t
    now_fn = now_fn or _t.monotonic
    while True:
        with _scan_lock:
            ts = _scan_state["ts"]
            if (ts is not None and _scan_state["key"] == key
                    and now_fn() - ts < ttl):
                return {"result": _scan_state["result"], "cached": True,
                        "cache_age_s": round(now_fn() - ts, 1)}
            if _scan_inflight.is_set():
                _scan_inflight.clear()
                break
        if not _scan_inflight.wait(timeout=60):
            raise TimeoutError("broad scan in-flight timeout")
    try:
        result = fetch()
        with _scan_lock:
            _scan_state["ts"] = now_fn()
            _scan_state["result"] = result
            _scan_state["key"] = key
        return {"result": result, "cached": False, "cache_age_s": 0.0}
    finally:
        _scan_inflight.set()


def _reset_scan_cache() -> None:
    _scan_state["ts"], _scan_state["result"] = None, None
    _scan_state["key"] = None
    _scan_inflight.set()
