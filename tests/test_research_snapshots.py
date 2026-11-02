import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.research_snapshots as rs


def bar(day, i, c=2.0, v=1000, o=None, h=None, l=None):
    c = c or 2.0
    return SimpleNamespace(timestamp=datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc) + timedelta(minutes=i),
                           open=o or c, high=h or c, low=l or c, close=c, volume=v)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "ROOT", tmp_path / "research")
    return tmp_path / "research"


def test_snapshot_versioning_and_immutability(store):
    bars = [bar(0, i) for i in range(5)]
    m1 = rs.save_snapshot("AMC", "2026-01-15", bars, {"feed": "iex", "provisional": True})
    m2 = rs.save_snapshot("AMC", "2026-01-15", bars + [bar(0, 5)], {"feed": "iex"})
    assert m1["version"] == 1 and m2["version"] == 2
    assert rs.list_versions("AMC", "2026-01-15") == [1, 2]
    assert len(rs.load_snapshot("AMC", "2026-01-15", 1)) == 5   # v1 untouched
    assert len(rs.load_snapshot("AMC", "2026-01-15")) == 6     # latest = v2
    assert m1["sha256"] != m2["sha256"]


def test_atomic_no_partial_file(store, monkeypatch):
    monkeypatch.setattr(rs.os, "replace", lambda a, b: (_ for _ in ()).throw(IOError("disk")))
    with pytest.raises(IOError):
        rs.save_snapshot("X", "2026-01-15", [bar(0, 0)], {})
    assert not list(store.rglob("*.tmp"))
    assert rs.list_versions("X", "2026-01-15") == []


def test_t1_compare_detects_backfill(store):
    v1 = [bar(0, i) for i in range(4)]
    rs.save_snapshot("EVGO", "2026-11-02", v1, {})
    v2 = v1[:2] + [bar(0, 9, c=9.9)] + v1[2:] + [bar(0, 20)]
    rs.save_snapshot("EVGO", "2026-11-02", v2, {})
    diff = rs.compare_versions("EVGO", "2026-11-02", 1, 2)
    assert not diff["identical"]
    assert len(diff["added"]) == 2 and diff["removed"] == []
    same = rs.compare_versions("EVGO", "2026-11-02", 2, 2)
    assert same["identical"]


def test_coverage_stats_and_low_confidence():
    # 200 observed of 390 expected -> 51% -> low
    bars = [SimpleNamespace(timestamp=datetime(2026,1,15,9,30,tzinfo=timezone.utc)+timedelta(minutes=i)) for i in range(200)]
    s = rs.coverage_stats(bars, 390)
    assert s["coverage_pct"] == 51.3 and s["confidence"] == "low"
    # gap counting: 10-minute hole between 9:39 and 9:50
    b2 = bars[:10] + bars[20:]
    s2 = rs.coverage_stats(b2, 390)
    assert s2["gaps_over_3m"] == 1 and s2["gaps"][0]["minutes"] == 10


def test_screening_tiers_point_in_time():
    # 12 baseline days then: strict day, approx day, nonqualifier
    days = []
    px = 2.0
    for i in range(12):
        days.append(SimpleNamespace(timestamp=datetime(2026,1,1+i,tzinfo=timezone.utc),
                                    open=px, high=px, low=px, close=px, volume=1000))
    days.append(SimpleNamespace(timestamp=datetime(2026,1,13,tzinfo=timezone.utc),
                                open=2.0, high=2.3, low=1.99, close=2.3, volume=10000))  # +15%, 10x
    days.append(SimpleNamespace(timestamp=datetime(2026,1,14,tzinfo=timezone.utc),
                                open=2.3, high=2.5, low=2.2, close=2.5, volume=3000))   # +8.7%
    out = rs.screen_daily_bars({"EVGO": days})
    tiers = {c.session_date: c.tier for c in out}
    assert tiers["2026-01-13"] == "ross_numeric_candidate"
    assert tiers["2026-01-14"] == "research_approx"
    # baseline days are recorded as nonqualifiers — no silent exclusion
    assert any(c.tier == "nonqualifier" for c in out)
    assert all(c.float_verified == "unverified" for c in out)
    # RVOL uses only PRIOR days — the +15% day's own vol can't inflate its baseline
    strict = next(c for c in out if c.tier == "ross_numeric_candidate")
    assert strict.rvol == pytest.approx(10.0)


def test_config_hash_and_canonical_result():
    from app.strategies.momentum_detector import MomentumSetupConfig
    h1 = rs.config_hash(MomentumSetupConfig())
    assert rs.config_hash(MomentumSetupConfig()) == h1
    assert rs.config_hash(MomentumSetupConfig(impulse_min_pct=6.0)) != h1
    r1 = {"setups": 1, "run_ts": "a", "git_commit": "x", "pnl": -20.3}
    r2 = {"setups": 1, "run_ts": "b", "git_commit": "y", "pnl": -20.3}
    assert rs.canonical_result(r1) == rs.canonical_result(r2)
