#!/usr/bin/env python3
"""Build the historical momentum research dataset (opt-in network use).

Usage:
  python scripts/build_research_set.py [--symbols AMC,EVGO] [--days 30] [--acquire]

Without --acquire only the daily screening runs (no snapshots written).
Snapshots are versioned/immutable under data/research/ (gitignored).
"""

import argparse
import json
import subprocess
from datetime import datetime, timedelta, timezone

from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from app.market_data import get_data_client
from app.market_discovery import get_trading_client
from app.intraday_data import (
    fetch_minute_bars, completed_minute_bars, filter_regular_session_bars)
from app.api.main import _fetch_trading_days
from app.research_snapshots import (
    ResearchScreenConfig, screen_daily_bars, save_screening,
    save_snapshot, coverage_stats, save_result, config_hash, canonical_result)
from app.strategies.momentum_detector import (
    MomentumSetupConfig, detect_momentum_setups)
from app.strategies.momentum_simulator import simulate_momentum_setups
from app.strategies.trade_simulator import TradeConfig

DEFAULT_UNIVERSE = [
    "AMC","GME","NVAX","EVGO","SPCX","FUBO","SIRI","PLUG","SPCE","RUM",
    "ACHR","MVIS","KULR","SOUN","ONDS","SERV","RKLB","ASTS","IONQ","RGTI",
    "CLOV","AUPH","MVST","QS","LMND","DNA","JOBY","SMR","OKLO","NNE",
    "LTBR","RR","WULF","BTDR","HIVE","IREN","CIFR","CLSK","MARA","RIOT",
    "SOS","CAN","EBON","GRAB","TELL","SMMT","VAXX","BNTC","PRAX","BBIO",
]


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(DEFAULT_UNIVERSE))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--acquire", action="store_true",
                    help="fetch minute bars and write snapshots")
    args = ap.parse_args()
    universe = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    now = datetime.now(timezone.utc)
    dc, tc = get_data_client(), get_trading_client()

    # ---- daily screening (post-session; never intraday claims) ----
    req = StockBarsRequest(symbol_or_symbols=universe, timeframe=TimeFrame.Day,
                           start=now - timedelta(days=args.days + 15), end=now,
                           feed="iex")
    daily = dc.get_stock_bars(req).data
    print(f"screened {len(daily)} symbols / {args.days}+15d")
    cfg = ResearchScreenConfig()
    cands = screen_daily_bars(daily, cfg)
    qual = [c for c in cands if c.tier != "nonqualifier"]
    save_screening(now.date().isoformat(), cands, cfg)
    print(f"candidates: {len(cands)} | qualifying: {len(qual)}")
    for c in qual:
        print(f"  {c.tier:24s} {c.symbol:6s} {c.session_date} "
              f"gain={c.gain_pct}% rvol={c.rvol}x ${c.close}")

    if not args.acquire:
        print("(dry run — pass --acquire to write snapshots)")
        return

    # ---- snapshots + detection on frozen data ----
    td = _fetch_trading_days(tc, (now - timedelta(days=args.days + 15)).date(), now.date())
    for c in qual:
        delta = (now.date() - datetime.fromisoformat(c.session_date).date()).days
        same_day = delta == 0
        if delta > args.days + 5:
            continue
        try:
            raw = fetch_minute_bars(dc, [c.symbol], days=delta + 3).get(c.symbol, [])
        except Exception as exc:
            print(f"  {c.symbol} {c.session_date}: acquisition FAILED {exc}")
            continue
        allr = filter_regular_session_bars(
            completed_minute_bars(raw, now=now), td)
        bars = [b for b in allr if b.timestamp.date().isoformat() == c.session_date]
        exp = 210 if td and td.get(datetime.fromisoformat(c.session_date).date(),
                                   None) and td[datetime.fromisoformat(
                                       c.session_date).date()][1].hour == 13 else 390
        cov = coverage_stats(bars, exp)
        meta = save_snapshot(c.symbol, c.session_date, bars, {
            "feed": "iex", "days_param": delta + 3,
            "calendar_verified": td is not None,
            "provisional": same_day,
            "candidate_tier": c.tier, "gain_pct": c.gain_pct,
            "rvol": c.rvol, **cov})
        print(f"  {c.symbol} {c.session_date}: v{meta['version']} "
              f"bars={len(bars)} cov={cov['coverage_pct']}% ({cov['confidence']}) "
              f"{'PROVISIONAL' if same_day else ''}")

        setups, rejected = detect_momentum_setups(bars, symbol=c.symbol)
        sim = simulate_momentum_setups(
            bars, setups,
            TradeConfig(risk_reward=2.0, require_contiguous_entry=True),
            session_times=td)
        result = {
            "symbol": c.symbol, "session": c.session_date,
            "tier": c.tier,
            "config_hash": config_hash(MomentumSetupConfig()),
            "git_commit": git_commit(),
            "snapshot_version": meta["version"],
            "snapshot_sha256": meta["sha256"],
            "feed": "iex",
            "sim_assumptions": {
                "entry_slippage_bps": 5.0, "exit_slippage_bps": 5.0,
                "commission": 0.0, "risk_reward": 2.0,
                "entry_rule": "next_completed_bar_open",
                "require_contiguous_entry": True,
            },
            "confirmed": len(setups),
            "rejected_by_reason": {
                r: sum(1 for x in rejected if x.reason == r)
                for r in {x.reason for x in rejected}},
            "trades": len(sim.trades),
            "unresolved": len(sim.unresolved),
            "net_pnl": sim.net_pnl,
            "run_ts": now.isoformat(),
        }
        result["canonical"] = canonical_result(result)
        save_result(c.symbol, c.session_date, meta["version"], result)
        print(f"    -> setups={len(setups)} trades={len(sim.trades)} "
              f"pnl={sim.net_pnl} canonical={result['canonical'][:12]}")


if __name__ == "__main__":
    main()
