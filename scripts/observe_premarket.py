#!/usr/bin/env python3
"""Premarket discovery observation — 5 scheduled ET captures per session.

Read-only research: measures whether gap_scan finds qualifying small-cap
gappers earlier than most_actives, and how fresh the IEX observations are.

Run: PYTHONPATH=. .venv/bin/python scripts/observe_premarket.py [--today]
Stays running through the window; safe to interrupt (partial results saved).
"""

import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
BASE = "http://127.0.0.1:8000"
OUT = Path(__file__).resolve().parent.parent / "data" / "research" / "premarket_live"
SLOTS = [(7, 0), (8, 0), (8, 30), (9, 0), (9, 20)]
TIMEOUT = 120


def due_slots(day: date, slots=SLOTS) -> list[datetime]:
    return [datetime.combine(day, datetime.min.time(), tzinfo=ET)
            + timedelta(hours=h, minutes=m) for h, m in slots]


def observe_once(url: str = f"{BASE}/api/scanner/premarket",
                 params: dict | None = None) -> dict:
    """One scheduled observation — never crashes, returns a record."""
    rec = {"requested_ts": datetime.now(ET).isoformat(),
           "status": None, "error": None, "response": None}
    try:
        r = requests.get(url, params=params or {"scan": "true"}, timeout=TIMEOUT)
        rec["status"] = r.status_code
        rec["response"] = r.json()
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
    return rec


def update_history(history: dict, rows: list, obs_ts: str) -> None:
    """Track per-symbol first_seen / first_qualified / sources."""
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        h = history.setdefault(sym, {
            "first_seen": None, "first_source": None,
            "first_qualified_ts": None, "first_gap": None,
            "sources": [], "observations": []})
        if h["first_seen"] is None:
            h["first_seen"] = obs_ts
            h["first_source"] = r.get("source")
        src = r.get("source")
        if src and src not in h["sources"]:
            h["sources"].append(src)
        if r.get("qualified") and h["first_qualified_ts"] is None:
            h["first_qualified_ts"] = obs_ts
            h["first_gap"] = r.get("gap_pct")
        h["observations"].append({
            "ts": obs_ts, "source": src, "last": r.get("last"),
            "gap_pct": r.get("gap_pct"), "age_s": r.get("age_s"),
            "status": r.get("status"), "rvol": r.get("rvol"),
            "qualified": r.get("qualified")})


def summarize(history: dict, records: list[dict]) -> dict:
    obs = []
    for rec in records:
        res = rec.get("response") or {}
        rows = res.get("rows") or []
        obs.append({
            "scheduled": rec.get("scheduled"),
            "requested_ts": rec.get("requested_ts"),
            "missed": rec.get("missed", False),
            "status": rec.get("status"), "error": rec.get("error"),
            "market": res.get("market"),
            "scan": res.get("scan"),
            "rows": len(rows),
            "gap_scan_rows": sum(1 for r in rows if r.get("source") == "gap_scan"),
            "qualified": sum(1 for r in rows if r.get("qualified")),
            "stale": sum(1 for r in rows if r.get("status") == "stale"),
            "no_prints": sum(1 for r in rows if r.get("status") == "no_iex_prints"),
            "rvol_ok": sum(1 for r in rows if r.get("rvol") is not None)})
    qualified_syms = [s for s, h in history.items() if h["first_qualified_ts"]]
    first_src = {}
    for s, h in history.items():
        k = h["first_source"] or "none"
        first_src[k] = first_src.get(k, 0) + 1
    later_ma = sum(1 for s, h in history.items()
                   if h["first_source"] == "gap_scan" and "most_actives" in h["sources"])
    return {
        "observations": obs,
        "unique_symbols": len(history),
        "qualified_symbols": sorted(qualified_syms),
        "qualified_count": len(qualified_syms),
        "first_source_counts": first_src,
        "gap_scan_later_in_most_actives": later_ma,
        "errors": [o for o in obs if o["error"] or (o["status"] not in (200, None))],
        "note": "single-session observation — not predictive performance",
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--slots", default=None,
                    help="HH:MM,HH:MM override (testing)")
    args = ap.parse_args()
    slots = SLOTS
    if args.slots:
        slots = [tuple(map(int, p.split(":"))) for p in args.slots.split(",")]
    try:
        requests.get(f"{BASE}/api/alerts", timeout=5)
    except Exception:
        sys.exit("backend unreachable on 127.0.0.1:8000")

    OUT.mkdir(parents=True, exist_ok=True)
    day = datetime.now(ET).date()
    history: dict = {}
    records: list[dict] = []
    hist_path = OUT / f"discovery_history_{day.isoformat()}.json"
    sum_path = OUT / f"pm_scan_summary_{day.isoformat()}.json"
    if hist_path.exists():                      # resume after interruption
        history = json.loads(hist_path.read_text())

    for target in due_slots(day, slots):
        wait = (target - datetime.now(ET)).total_seconds()
        if wait > 0:
            print(f"[{datetime.now(ET).strftime('%H:%M:%S')}] sleep {wait/60:.0f}m "
                  f"-> {target.strftime('%H:%M')} ET")
            time.sleep(wait)
        rec = observe_once()
        rec["scheduled"] = target.isoformat()
        rec["missed"] = datetime.now(ET) > target + timedelta(minutes=5)
        records.append(rec)
        rows = (rec.get("response") or {}).get("rows") or []
        update_history(history, rows, rec["requested_ts"])
        path = OUT / f"pm_scan_{day.isoformat()}_{target.strftime('%H%M')}.json"
        path.write_text(json.dumps(rec, indent=1))
        hist_path.write_text(json.dumps(history, indent=1))
        s = (rec.get("response") or {}).get("scan") or {}
        print(f"[{target.strftime('%H:%M')}] rows={len(rows)} scan_cands={s.get('candidates')} "
              f"cached={s.get('cached')} err={rec['error']}")

    summary = summarize(history, records)
    sum_path.write_text(json.dumps(summary, indent=1))
    print(f"\nSUMMARY: {summary['unique_symbols']} syms, "
          f"{summary['qualified_count']} qualified "
          f"({', '.join(summary['qualified_symbols'][:8])})")
    print(f"first_source: {summary['first_source_counts']} | "
          f"gap_scan->most_actives later: {summary['gap_scan_later_in_most_actives']}")
    print(f"saved: {sum_path.name} + {len(records)} raw captures")


if __name__ == "__main__":
    main()
