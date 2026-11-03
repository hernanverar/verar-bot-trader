from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from app.premarket_scanner import (
    PremarketCfg, pm_bounds, elapsed_window, filter_pm, premarket_rvol,
    symbol_metrics, dedup_symbols, prev_rth_close, baseline_volumes,
    MIN_BASELINE_SESSIONS, MIN_BASELINE_VOLUME)

ET = ZoneInfo("America/New_York")
DAY = datetime(2026, 11, 3).date()       # Tuesday


def bar(hh, mm, o=2.0, h_=2.1, l=1.9, c=2.05, v=1000, day=DAY):
    ts = datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET)
    return SimpleNamespace(timestamp=ts, open=o, high=h_, low=l, close=c, volume=v)


def test_pm_window_boundaries():
    start, end = pm_bounds(DAY)
    assert start.time() == time(4, 0) and end.time() == time(9, 30)
    now_et = datetime(2026, 11, 3, 8, 0, tzinfo=ET)
    s, e = elapsed_window(DAY, now_et)
    assert e == now_et  # elapsed window ends at now
    s2, e2 = elapsed_window(DAY, datetime(2026, 11, 3, 10, 0, tzinfo=ET))
    assert e2 == end  # capped at 9:30


def test_filter_pm_excludes_rth_and_overnight():
    now = datetime(2026, 11, 3, 8, 0, tzinfo=ET)
    bars = [bar(3, 59), bar(4, 0), bar(9, 29), bar(9, 30), bar(10, 0)]
    pm = filter_pm(bars, DAY, now)
    times = [b.timestamp.strftime("%H:%M") for b in pm]
    assert times == ["04:00"]  # only bars up to now are in window


def test_gap_and_prev_close():
    daily = [SimpleNamespace(timestamp=datetime(2026, 10, 30, 16, tzinfo=ET), close=2.0),
             SimpleNamespace(timestamp=datetime(2026, 11, 2, 16, tzinfo=ET), close=2.5)]
    sessions = [datetime(2026, 10, 30).date(), datetime(2026, 11, 2).date(), DAY]
    pc, pcd = prev_rth_close(daily, sessions, DAY)
    assert pc == 2.5 and pcd == datetime(2026, 11, 2).date()
    m = symbol_metrics([bar(8, 0, c=3.0, v=500)], pc, pcd, [], 
                       datetime(2026, 11, 3, 8, 0, 1, tzinfo=ET), PremarketCfg())
    assert m["gap_pct"] == 20.0 and m["qualified"] is True


def test_no_prints_and_stale():
    now = datetime(2026, 11, 3, 9, 0, tzinfo=ET)
    m = symbol_metrics([], 2.0, DAY, [], now, PremarketCfg())
    assert m["status"] == "no_iex_prints" and m["last"] is None
    old = bar(6, 0, c=3.0, v=100)
    m2 = symbol_metrics([old], 2.0, DAY, [], now, PremarketCfg())
    assert m2["status"] == "stale"


def test_rvol_baseline_rules():
    # insufficient sessions
    r = premarket_rvol(5000, [MIN_BASELINE_VOLUME * 2] * 2)
    assert r["rvol"] is None and r["status"] == "insufficient_baseline"
    # near-zero denominators filtered out -> insufficient
    r2 = premarket_rvol(5000, [10, 20, 30])
    assert r2["status"] == "insufficient_baseline"
    # valid baseline
    r3 = premarket_rvol(5000, [1000, 1000, 2000, 500])
    assert r3["rvol"] == 5.0 and r3["baseline_n"] == 4
    # zero-volume sessions never count
    r4 = premarket_rvol(5000, [0, 0, 1000, 1000, 1000])
    assert r4["baseline_n"] == 3 and r4["rvol"] == 5.0


def test_same_window_baseline():
    now = datetime(2026, 11, 3, 7, 30, tzinfo=ET)  # 3.5h elapsed
    d1, d2 = datetime(2026, 11, 2).date(), datetime(2026, 10, 30).date()
    hist = {d1.isoformat(): [bar(4, 0, v=100, day=d1), bar(7, 0, v=200, day=d1),
                             bar(9, 45, v=999, day=d1)],  # outside window
            d2.isoformat(): [bar(4, 0, v=50, day=d2)]}
    vols = baseline_volumes(hist, [d1, d2, DAY], DAY, now, 5)
    assert vols == [50, 300]  # 9:45 bar excluded from d1


def test_dedup_sources():
    rows = dedup_symbols([("aapl", "most_actives"), ("AAPL", "watchlist"),
                          ("gme", "watchlist")])
    assert rows == [("AAPL", "most_actives"), ("GME", "watchlist")]


def test_missing_prev_close():
    now = datetime(2026, 11, 3, 9, 0, tzinfo=ET)
    m = symbol_metrics([bar(8, 0, c=2.0, v=500)], None, None, [], now, PremarketCfg())
    assert m["status"] == "missing_prev_close" and m["qualified"] is False


def test_prev_close_monday_and_holiday():
    # Monday: prior session is Friday; a holiday mid-week is skipped
    fri, mon = datetime(2026, 10, 30).date(), datetime(2026, 11, 2).date()
    daily = [SimpleNamespace(timestamp=datetime(2026, 10, 29, 16, tzinfo=ET), close=9.9),
             SimpleNamespace(timestamp=datetime(2026, 10, 30, 16, tzinfo=ET), close=2.5)]
    pc, pcd = prev_rth_close(daily, [fri, mon], mon)
    assert pc == 2.5 and pcd == fri
    # incomplete current-day bar never used
    daily.append(SimpleNamespace(timestamp=datetime(2026, 11, 2, 12, tzinfo=ET), close=3.3))
    pc2, _ = prev_rth_close(daily, [fri, mon], mon)
    assert pc2 == 2.5


def test_qualified_needs_price_and_gap_only():
    now = datetime(2026, 11, 3, 8, 0, tzinfo=ET)
    # gap ok but thin volume -> still qualified (volume is evidence, not filter)
    m = symbol_metrics([bar(8, 0, c=5.0, v=10)], 4.0, DAY, [], now, PremarketCfg())
    assert m["qualified"] is True
    # out of price band -> not qualified
    m2 = symbol_metrics([bar(8, 0, c=15.0, v=10)], 4.0, DAY, [], now, PremarketCfg())
    assert m2["qualified"] is False


def test_gap_scan_from_snapshots():
    from app.premarket_scanner import gap_scan
    snaps = {
        "GOOD": {"latestTrade": {"p": 3.0, "t": "t1"}, "prevDailyBar": {"c": 2.0}},
        "EXP":  {"latestTrade": {"p": 15.0, "t": "t1"}, "prevDailyBar": {"c": 10.0}},
        "DOWN": {"latestTrade": {"p": 1.5, "t": "t1"}, "prevDailyBar": {"c": 2.0}},
        "NOD":  {"latestTrade": None, "prevDailyBar": {"c": 2.0}},
        "SMALL":{"latestTrade": {"p": 2.0, "t": "t1"}, "prevDailyBar": {"c": 1.95}},
    }
    rows = gap_scan(snaps, PremarketCfg(), discover_gap_pct=5.0)
    syms = [r["symbol"] for r in rows]
    assert syms == ["GOOD"]  # EXP out of band, DOWN negative, NOD none, SMALL 2.6%
    assert rows[0]["gap_pct"] == 50.0 and rows[0]["source"] == "gap_scan"


def test_gap_scan_rejects_stale_trades():
    from app.premarket_scanner import gap_scan
    pm_open = datetime(2026, 11, 3, 4, 0, tzinfo=ET)
    snaps = {
        "FRESH": {"latestTrade": {"p": 3.0, "t": "2026-11-03T13:30:00Z"},
                  "prevDailyBar": {"c": 2.0}},
        "OLD":   {"latestTrade": {"p": 3.0, "t": "2026-11-02T20:00:00Z"},  # yesterday
                  "prevDailyBar": {"c": 2.0}},
        "BADTS": {"latestTrade": {"p": 3.0, "t": "garbage"},
                  "prevDailyBar": {"c": 2.0}},
    }
    rows = gap_scan(snaps, PremarketCfg(), valid_after=pm_open)
    assert [r["symbol"] for r in rows] == ["FRESH"]
    # without a window, OLD would pass -> validates the guard exists
    assert len(gap_scan(snaps, PremarketCfg())) == 3


def test_scan_cache_ttl_and_concurrency():
    from app.premarket_scanner import cached_gap_scan, _reset_scan_cache
    _reset_scan_cache()
    calls = []
    t = [0.0]
    def fetch(): calls.append(1); return {"stats": {"scanned": 1}, "found": []}
    r1 = cached_gap_scan(fetch, now_fn=lambda: t[0])
    r2 = cached_gap_scan(fetch, now_fn=lambda: t[0] + 10)
    assert len(calls) == 1 and r1["cached"] is False and r2["cached"] is True
    t[0] = 300.0
    r3 = cached_gap_scan(fetch, now_fn=lambda: t[0], ttl=240)
    assert r3["cached"] is False and len(calls) == 2
    # concurrency: concurrent callers share in-flight result
    _reset_scan_cache()
    import threading, time
    out = []
    def slow(): import time; time.sleep(0.2); return {"stats": {}, "found": ["X"]}
    t1 = threading.Thread(target=lambda: out.append(cached_gap_scan(slow)))
    t2 = threading.Thread(target=lambda: out.append(cached_gap_scan(slow)))
    t1.start(); time.sleep(0.05); t2.start(); t1.join(); t2.join()
    # second caller waited for the in-flight scan
    assert len(out) == 2


def test_cache_key_invalidates_and_failure_recovers():
    from app.premarket_scanner import cached_gap_scan, _reset_scan_cache
    _reset_scan_cache()
    n = [0]
    def fetch(): n[0] += 1; return {"found": []}
    cached_gap_scan(fetch, key="day1|g5")
    assert cached_gap_scan(fetch, key="day1|g10")["cached"] is False  # new key
    assert n[0] == 2
    # failure releases in-flight -> next call retries fresh
    def boom(): raise RuntimeError("api down")
    _reset_scan_cache()
    try: cached_gap_scan(boom, key="k")
    except RuntimeError: pass
    assert cached_gap_scan(fetch, key="k")["result"] == {"found": []}
