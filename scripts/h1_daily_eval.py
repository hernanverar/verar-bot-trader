#!/usr/bin/env python3
"""Prospective H1 validation — one completed trading day at a time.

Frozen protocol (h1_validation_manifest.json):
- Only sessions on/after VALIDATION_START (2026-11-03) are eligible.
- Idempotent: a (symbol,date) already evaluated is never reprocessed.
- Incomplete/current sessions are rejected.
- Baseline vs H1 evaluated on identical SIP snapshots.

Usage: PYTHONPATH=. .venv/bin/python scripts/h1_daily_eval.py --date 2026-11-03
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone, date

import requests
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.market_data import get_data_client
from app.market_discovery import get_trading_client
from app.api.main import _fetch_trading_days
from app.intraday_data import (
    completed_minute_bars, filter_regular_session_bars)
from app.research_snapshots import (
    ResearchScreenConfig, screen_daily_bars, save_snapshot,
    coverage_stats, config_hash, canonical_result, save_result)
from app.strategies.momentum_detector import (
    MomentumSetupConfig, detect_momentum_setups)
from app.strategies.momentum_simulator import simulate_momentum_setups
from app.strategies.hold_confirmation import evaluate_h1
from app.strategies.trade_simulator import TradeConfig
from app.asset_universe import fetch_asset_universe

VALIDATION_START = date(2026, 11, 3)
MANIFEST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "research", "h1_validation_manifest.json")
BAD_NAME = ("warrant", "unit", "right", "preferred", " note", " etf")


def load_manifest() -> dict:
    if not os.path.exists(MANIFEST):
        return {"protocol": {
                    "validation_start": VALIDATION_START.isoformat(),
                    "config_hash": config_hash(MomentumSetupConfig()),
                    "min_coverage_pct": 80,
                    "min_h1_fills_for_conclusion": 30,
                    "pre_registered": True},
                "sessions": {}}
    return json.load(open(MANIFEST))


def save_manifest(m: dict) -> None:
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    tmp = MANIFEST + ".tmp"
    json.dump(m, open(tmp, "w"), indent=1)
    os.replace(tmp, MANIFEST)


def validate_day(day: date) -> str | None:
    """Reject ineligible dates — returns reason or None if OK."""
    if day < VALIDATION_START:
        return "before validation start"
    today = datetime.now(timezone.utc).date()
    if day >= today:
        return "session not complete (today or future)"
    if day.weekday() >= 5:
        return "weekend"
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    args = ap.parse_args()
    day = datetime.fromisoformat(args.date).date()
    reason = validate_day(day)
    if reason:
        print(f"REJECTED {day}: {reason}")
        return
    manifest = load_manifest()
    dc, tc = get_data_client(), get_trading_client()
    td = _fetch_trading_days(tc, day, day)
    if not td or day not in td:
        print(f"REJECTED {day}: not a trading session (calendar)")
        save_manifest(manifest)
        return
    exp = 210 if td[day][1].hour == 13 else 390

    now = datetime.now(timezone.utc)
    end = min(now - timedelta(minutes=20),
              datetime(day.year, day.month, day.day, 23, 59, tzinfo=timezone.utc))
    # 1) refreshed universe + warrant filter
    syms, _ = fetch_asset_universe()
    env = {k.strip(): v.strip().strip('"')
           for l in open(".env") if "=" in l for k, v in [l.split("=", 1)]}
    h = {"APCA-API-KEY-ID": env["ALPACA_API_KEY"],
         "APCA-API-SECRET-KEY": env["ALPACA_SECRET_KEY"]}
    names = {a["symbol"]: a.get("name", "")
             for a in requests.get(
                 "https://paper-api.alpaca.markets/v2/assets",
                 params={"asset_class": "us_equity", "status": "active"},
                 headers=h, timeout=30).json()}
    syms = [s for s in syms
            if not any(b in names.get(s, "").lower() for b in BAD_NAME)]

    # 2) daily screen for THIS session only (needs ~11d history)
    daily = {}
    for chunk in [syms[i:i+120] for i in range(0, len(syms), 120)]:
        res = dc.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
            start=end - timedelta(days=16), end=end, feed="sip"))
        daily.update(res.data)
    cands = [c for c in screen_daily_bars(daily, ResearchScreenConfig())
             if c.tier == "ross_numeric_candidate"
             and c.session_date == day.isoformat()]
    print(f"{day}: {len(cands)} ross candidates")

    manifest["sessions"].setdefault(day.isoformat(), {
        "expected_minutes": exp, "candidates": [], "excluded": {}})

    # 3) acquire + paired eval, idempotent by (sym, day)
    sess = manifest["sessions"][day.isoformat()]
    done = {r["sym"] for r in sess["candidates"]}
    for c in cands:
        if c.symbol in done:
            continue
        try:
            req = StockBarsRequest(symbol_or_symbols=[c.symbol],
                                   timeframe=TimeFrame.Minute,
                                   start=end - timedelta(days=4), end=end,
                                   feed="sip")
            raw = list(dc.get_stock_bars(req).data.get(c.symbol, []))
        except Exception as exc:
            sess["excluded"].setdefault("api_failures", []).append(
                {"sym": c.symbol, "err": str(exc)[:100]})
            continue
        bars = [b for b in filter_regular_session_bars(
                completed_minute_bars(raw, now=now), td)
                if b.timestamp.date() == day]
        cov = coverage_stats(bars, exp)
        if cov["coverage_pct"] < 80:
            sess["excluded"].setdefault("low_coverage", []).append(
                {"sym": c.symbol, "cov": cov["coverage_pct"]})
            continue
        meta = save_snapshot(c.symbol, day.isoformat(), bars, {
            "calendar_verified": True, "provisional": False,
            "tier": "ross_numeric_candidate", **cov}, feed="sip")
        setups, _ = detect_momentum_setups(bars, symbol=c.symbol)
        base = simulate_momentum_setups(
            bars, setups,
            TradeConfig(risk_reward=2.0, require_contiguous_entry=True),
            session_times=td)
        h1, out = evaluate_h1(
            bars, setups,
            TradeConfig(risk_reward=2.0, require_contiguous_entry=True),
            session_times=td)
        bw = sum(1 for t in base.trades if t.net_pnl > 0)
        hw = sum(1 for t in h1.trades if t.net_pnl > 0)
        rec = {"sym": c.symbol, "sha": meta["sha256"], "cov": cov["coverage_pct"],
               "gain": c.gain_pct, "rvol": c.rvol, "setups": len(setups),
               "base_fills": len(base.trades), "base_wins": bw,
               "base_pnl": base.net_pnl,
               "h1_fills": len(h1.trades), "h1_wins": hw,
               "h1_pnl": h1.net_pnl,
               "outcomes": {str(k): v for k, v in out.items()},
               "acquired_ts": now.isoformat()}
        sess["candidates"].append(rec)
        print(f"  {c.symbol}: setups={len(setups)} "
              f"base={len(base.trades)}/{base.net_pnl:.2f} "
              f"h1={len(h1.trades)}/{h1.net_pnl:.2f}")
    save_manifest(manifest)

    # 4) running totals across all validation days
    allr = [r for s in manifest["sessions"].values() for r in s["candidates"]]
    ts, bf, hf = sum(r["setups"] for r in allr), \
        sum(r["base_fills"] for r in allr), sum(r["h1_fills"] for r in allr)
    bp, hp = sum(r["base_pnl"] for r in allr), sum(r["h1_pnl"] for r in allr)
    print(f"\nVALIDATION TOTALS days={len(manifest['sessions'])} "
          f"setups={ts} base={bf} fills/${bp:.2f} h1={hf} fills/${hp:.2f}")
    if ts:
        print(f"exp/setup: base={bp/ts:.2f} h1={hp/ts:.2f}")
    if bf:
        print(f"exp/fill:  base={bp/bf:.2f}")
    # Pre-registered verdict: all three conditions required for PASS
    if hf < 30:
        verdict = "INCONCLUSIVE (<30 h1 fills)"
    elif hp / hf > 0 and ts and hp / ts > bp / ts:
        verdict = "PASS"
    else:
        verdict = "FAIL"
    if hf:
        print(f"exp/fill:  h1={hp/hf:.2f}")
    print(f"VERDICT: {verdict}")


if __name__ == "__main__":
    main()
