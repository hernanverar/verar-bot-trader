"""Session diagnostics — append-only JSONL, local-only, never fatal.

Schema-constrained event recording for supervised validation sessions.
Files: data/diagnostics/YYYY-MM-DD.jsonl keyed by ET session date.
Logging failures are swallowed — diagnostics must never break research.
"""

import json
import os
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")
STORE_DIR = Path(__file__).resolve().parent.parent / "data" / "diagnostics"
MAX_FILE_BYTES = 10 * 1024 * 1024
RETENTION_DAYS = 30
_lock = threading.Lock()

# Allowed fields per event kind — arbitrary payloads are NOT accepted.
_SCHEMAS: dict[str, set[str]] = {
    "scanner_eval": {"symbols", "eligible", "research", "filtered", "unavailable", "session", "calendar_verified", "duration_ms"},
    "chart_refresh": {"symbol", "interval_min", "ok", "fetch_ms", "last_data_ts", "data_age_ms", "bars", "error", "session"},
    "alpaca_error": {"context", "symbol", "error"},
    "alert_eval": {"alerts_evaluated", "monitored_symbols", "skipped_symbols", "new_events", "busy", "duration_ms"},
    "alert_triggered": {"alert_id", "symbol", "alert_type", "bar_ts", "eval_ts", "alert_latency_ms", "trigger_price", "replay"},
    "sim_run": {"symbol", "patterns_confirmed", "trades_entered", "unresolved", "net_pnl", "exit_reasons", "duration_ms"},
    "client_event": {"client_kind", "event_id", "symbol", "reported_at", "detail"},
    "session_gap": {"symbol", "last_data_ts", "gap_ms", "observed_at"},
    "api_error": {"context", "error"},
}

_SENSITIVE_KEY = re.compile(r"(?i)(api[_-]?key|secret|token|password|authoriz|credential)")


def _redact(fields: dict) -> dict:
    return {
        k: ("<redacted>" if _SENSITIVE_KEY.search(k) else v)
        for k, v in fields.items()
    }


def record(kind: str, **fields) -> None:
    """Append one structured event. Never raises into callers."""
    try:
        allowed = _SCHEMAS.get(kind)
        if allowed is None:
            return
        clean = _redact({k: v for k, v in fields.items() if k in allowed})
        now = datetime.now(tz=MARKET_TZ)
        rec = {
            "ts_utc": datetime.now(tz=ZoneInfo("UTC")).replace(microsecond=0).isoformat(),
            "kind": kind,
            "session_date": now.date().isoformat(),
            **clean,
        }
        _append(now.date(), rec)
    except Exception:
        pass


def _append(day: date, rec: dict) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    path = STORE_DIR / f"{day.isoformat()}.jsonl"
    line = json.dumps(rec, default=str) + "\n"
    with _lock:
        if path.exists() and path.stat().st_size > MAX_FILE_BYTES:
            return  # bounded: stop appending rather than grow unbounded
        with path.open("a") as f:
            f.write(line)
        _prune_old_files()


def _prune_old_files() -> None:
    cutoff = datetime.now(tz=MARKET_TZ).date() - timedelta(days=RETENTION_DAYS)
    for p in STORE_DIR.glob("*.jsonl"):
        try:
            if date.fromisoformat(p.stem) < cutoff:
                p.unlink()
        except ValueError:
            continue


def load_day(day: date) -> list[dict]:
    path = STORE_DIR / f"{day.isoformat()}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # corrupt record skipped, others survive
    return out


def _completeness(day: date, recs: list[dict], calendar) -> dict:
    """Observed vs official session — partial sessions are flagged, totals
    are never presented as exhaustive when retention/limits may have
    dropped observations."""
    out: dict = {"partial": True, "notes": []}
    path = STORE_DIR / f"{day.isoformat()}.jsonl"
    if path.exists() and path.stat().st_size >= MAX_FILE_BYTES:
        out["notes"].append("file size cap reached — later events may have been dropped")
    out["notes"].append("files older than 30 days are pruned — history is not exhaustive")
    if calendar and recs:
        try:
            first = datetime.fromisoformat(recs[0]["ts_utc"])
            last = datetime.fromisoformat(recs[-1]["ts_utc"])
            session_open = datetime.combine(day, calendar[0], tzinfo=MARKET_TZ)
            session_close = datetime.combine(day, calendar[1], tzinfo=MARKET_TZ)
            session_span = (session_close - session_open).total_seconds()
            observed_span = (last - first).total_seconds()
            out["observed_span_s"] = round(observed_span)
            out["session_span_s"] = round(session_span)
            out["coverage_fraction_estimated"] = (
                round(min(observed_span / session_span, 1.0), 2)
                if session_span > 0 else None
            )
            out["partial"] = observed_span < session_span * 0.9
        except (ValueError, TypeError):
            out["coverage_fraction_estimated"] = None
    elif not recs:
        out["notes"].append("no observations recorded for this date")
    elif not calendar:
        out["notes"].append("official session hours unavailable — coverage cannot be computed")
    return out


def session_report(day: date, calendar: dict | None) -> dict:
    """Aggregate one session's events into a validation report."""
    recs = load_day(day)

    def _stats(values):
        vals = sorted(v for v in values if v is not None)
        if not vals:
            return {"available": False}
        mid = len(vals) // 2
        median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
        return {"available": True, "median": round(median, 1), "max": round(max(vals), 1), "count": len(vals)}

    scans = [r for r in recs if r["kind"] == "scanner_eval"]
    charts = [r for r in recs if r["kind"] == "chart_refresh"]
    evals = [r for r in recs if r["kind"] == "alert_eval"]
    triggers = [r for r in recs if r["kind"] == "alert_triggered"]
    sims = [r for r in recs if r["kind"] == "sim_run"]
    errors = [r for r in recs if r["kind"] == "alpaca_error" or r.get("error")]
    client_events = [r for r in recs if r["kind"] == "client_event"]
    skipped = sorted({s for r in evals for s in (r.get("skipped_symbols") or [])})

    return {
        "date": day.isoformat(),
        "observation_window": {
            "first_ts": recs[0]["ts_utc"] if recs else None,
            "last_ts": recs[-1]["ts_utc"] if recs else None,
            "available": bool(recs),
        },
        "completeness": _completeness(day, recs, calendar),
        "calendar": (
            {"verified": True, "open": calendar[0].isoformat(), "close": calendar[1].isoformat()}
            if calendar else {"verified": "unavailable"}
        ),
        "scanner_evaluations": {"measured": len(scans), "sessions": {r.get("session") for r in scans} and list({r.get("session") for r in scans if r.get("session")})},
        "chart_refreshes": {
            "measured": len(charts),
            "ok": sum(1 for r in charts if r.get("ok")),
            "failed": sum(1 for r in charts if not r.get("ok")),
        },
        "data_freshness_ms": _stats([r.get("data_age_ms") for r in charts]),
        "data_freshness_rth_ms": _stats(
            [r.get("data_age_ms") for r in charts if r.get("session") == "regular"]
        ),
        "data_freshness_non_rth_ms": _stats(
            [r.get("data_age_ms") for r in charts
             if r.get("session") not in (None, "regular")]
        ),
        "api_fetch_ms": _stats([r.get("fetch_ms") for r in charts]),
        "alert_evaluations": {
            "measured": len(evals),
            "busy": sum(1 for r in evals if r.get("busy")),
            "skipped_symbols": skipped,
        },
        "alert_events": {
            "raw_records": len(triggers),          # logged triggers incl. replays
            "unique_events": len({(t.get("alert_id"), t.get("bar_ts")) for t in triggers}),
            "replay_events": sum(1 for t in triggers if t.get("replay")),
            "alert_latency_ms": _stats(
                [r.get("alert_latency_ms") for r in triggers if not r.get("replay")]
            ),  # replay latency excluded — not live detection lag
        },
        "client_reported": {
            "count": len(client_events),
            "note": "browser-reported, not server-verified",
            "kinds": sorted({r.get("client_kind", "?") for r in client_events}),
        },
        "simulations": {
            "measured": len(sims),
            "trades": sum(r.get("trades_entered") or 0 for r in sims),
            "unresolved": sum(r.get("unresolved") or 0 for r in sims),
        },
        "errors": {
            "measured": len(errors),
            "items": [
                {"ts": r["ts_utc"], "context": r.get("context", r["kind"]), "error": str(r.get("error"))[:200]}
                for r in errors[-20:]
            ],
        },
    }
