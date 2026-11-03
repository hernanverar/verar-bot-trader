#!/usr/bin/env python3
"""Automated premarket scanner capture — Tue Nov 3, 2026.

Sleeps until each scheduled ET time, calls GET /api/scanner/premarket,
saves full responses under data/research/premarket_live/ (gitignored).

Run: PYTHONPATH=. .venv/bin/python scripts/capture_premarket.py
Must stay running during the window — it schedules itself.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
BASE = "http://127.0.0.1:8000"
OUT = Path(__file__).resolve().parent.parent / "data" / "research" / "premarket_live"
TIMES = [(8, 0), (8, 30), (9, 0)]


def backend_up() -> bool:
    try:
        return requests.get(f"{BASE}/api/alerts", timeout=5).status_code == 200
    except Exception:
        return False


def capture(day, hh, mm) -> dict:
    target = datetime.combine(day, datetime.min.time(), tzinfo=ET) + timedelta(hours=hh, minutes=mm)
    now = datetime.now(ET)
    rec = {"scheduled": target.isoformat(), "requested_ts": now.isoformat(),
           "missed": now > target + timedelta(minutes=5), "status": None,
           "error": None, "response": None}
    try:
        r = requests.get(f"{BASE}/api/scanner/premarket", timeout=60)
        rec["status"] = r.status_code
        rec["response"] = r.json()
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
    return rec


def summarize(rec: dict) -> str:
    rows = (rec.get("response") or {}).get("rows", [])
    q = sum(1 for r in rows if r.get("qualified"))
    np_ = sum(1 for r in rows if r.get("status") == "no_iex_prints")
    stale = sum(1 for r in rows if r.get("status") == "stale")
    rvol = sum(1 for r in rows if r.get("rvol") is not None)
    calls = (rec.get("response") or {}).get("api_calls", {})
    return (f"rows={len(rows)} qualified={q} no_prints={np_} stale={stale} "
            f"rvol={rvol} calls={calls} status={rec['status']} err={rec['error']}")


def main() -> None:
    day = datetime.now(ET).date()
    if not backend_up():
        sys.exit("backend unreachable on 127.0.0.1:8000 — start uvicorn first")
    OUT.mkdir(parents=True, exist_ok=True)
    for hh, mm in TIMES:
        target = datetime.combine(day, datetime.min.time(), tzinfo=ET) + timedelta(hours=hh, minutes=mm)
        wait = (target - datetime.now(ET)).total_seconds()
        if wait > 0:
            print(f"[{datetime.now(ET).strftime('%H:%M:%S')}] sleeping {wait/60:.0f}m until {hh:02d}:{mm:02d} ET")
            time.sleep(wait)
        rec = capture(day, hh, mm)
        path = OUT / f"pm_{day.isoformat()}_{hh:02d}{mm:02d}.json"
        path.write_text(json.dumps(rec, indent=1))
        print(f"[{hh:02d}:{mm:02d}] saved {path.name} | {summarize(rec)}")
    print("all captures complete")


if __name__ == "__main__":
    main()
