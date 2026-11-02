"""Six-month barcoding research for AMC and VIR (Apr 30 – Oct 30, 2026).

Fetches 1-minute OHLCV bars from Alpaca (paginated), filters to verified
regular sessions via the exchange calendar, counts completed non-overlapping
oscillation cycles at 3/5/10% thresholds and daily-range days, reports
coverage + data limitations, and writes a per-cycle CSV.

Read-only research. Run: .venv/bin/python scripts/barcoding_research.py
"""

import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median

sys.path.insert(0, ".")

from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from app.cycle_research import count_cycles, daily_ranges
from app.intraday_data import filter_regular_session_bars
from app.market_data import _data_feed, get_data_client
from app.market_discovery import get_trading_client
from app.market_session import _fetch_trading_days

SYMBOLS = ["AMC", "VIR"]
START = datetime(2026, 4, 30, tzinfo=timezone.utc)
END = datetime(2026, 10, 31, tzinfo=timezone.utc)
THRESHOLDS = [3.0, 5.0, 10.0]
PAGE_LIMIT = 10_000
CSV_PATH = "barcoding_cycles.csv"


def fetch_all_minute_bars(client, symbol, start, end):
    """All 1-minute bars for the window — the SDK paginates internally;
    an explicit `limit` would silently cap total results."""
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed=_data_feed(),
        sort="asc",
    )
    try:
        result = client.get_stock_bars(req)
    except Exception as exc:  # surface, don't fabricate
        return [], f"API error: {exc}"
    return result.data.get(symbol, []), None


def main():
    data_client = get_data_client()
    try:
        trading_client = get_trading_client()
    except RuntimeError:
        trading_client = None
    cal = _fetch_trading_days(trading_client, START.date(), END.date())

    print(f"Feed: {_data_feed().value}  |  Window: {START.date()} -> {END.date()}")
    print(f"Calendar: {'verified (%d trading days)' % len(cal) if cal else 'UNAVAILABLE'}")
    print()

    all_rows = []
    for symbol in SYMBOLS:
        bars, err = fetch_all_minute_bars(data_client, symbol, START, END)
        if err:
            print(f"{symbol}: fetch failed — {err}")
            continue
        regular = filter_regular_session_bars(bars, cal or {})
        sessions = sorted({b.timestamp.date() for b in regular})
        # ET session dates for reporting
        from app.market_data import MARKET_TZ
        sessions = sorted({b.timestamp.astimezone(MARKET_TZ).date() for b in regular})

        ranges = daily_ranges(regular)
        print(f"== {symbol} ==")
        print(f"raw bars: {len(bars):,}  regular-session bars: {len(regular):,}")
        print(f"trading sessions covered: {len(sessions)}  ({sessions[0] if sessions else 'n/a'} .. {sessions[-1] if sessions else 'n/a'})")
        if cal:
            expected = len(cal)
            covered = len(sessions)
            missing = sorted(set(cal) - set(sessions))
            print(f"expected sessions: {expected}  covered: {covered}  missing: {len(missing)}")
            if missing:
                print(f"  missing dates: {[str(m) for m in missing[:10]]}{' ...' if len(missing) > 10 else ''}")

        dollar_vol = [b.close * b.volume for b in regular if b.close and b.volume]
        if dollar_vol:
            print(f"median 1-min dollar volume: ${median(dollar_vol):,.0f}")

        for t in THRESHOLDS:
            qualifying_days = sum(1 for v in ranges.values() if v >= t)
            print(f"days with >= {t:g}% range: {qualifying_days}")

        for t in THRESHOLDS:
            cycles = count_cycles(regular, t, symbol)
            by_day = defaultdict(int)
            for c in cycles:
                by_day[c.session_date] += 1
            avg = len(cycles) / len(sessions) if sessions else 0
            mx = max(by_day.values()) if by_day else 0
            top = sorted(by_day.items(), key=lambda kv: -kv[1])[:3]
            print(f"cycles @{t:g}%: {len(cycles)}  avg/day {avg:.2f}  max/day {mx}  top: {top}")
            for c in cycles:
                all_rows.append([
                    c.symbol, c.threshold_pct, c.session_date,
                    c.start_ts.isoformat(), c.end_ts.isoformat(),
                    f"{c.start_price:.4f}", f"{c.pivot_price:.4f}", f"{c.end_price:.4f}",
                    c.amplitude_pct, f"{c.duration_min:.0f}",
                ])
        print()

    with open(CSV_PATH, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "threshold_pct", "session_date", "start_ts", "end_ts",
                    "start_price", "pivot_price", "end_price", "amplitude_pct", "duration_min"])
        w.writerows(all_rows)
    print(f"CSV written: {CSV_PATH} ({len(all_rows)} cycles)")


if __name__ == "__main__":
    main()
