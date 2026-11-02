import json
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import app.diagnostics as diag
import pytest

ET = ZoneInfo("America/New_York")
DAY = date(2026, 1, 15)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(diag, "STORE_DIR", tmp_path)
    return tmp_path


def test_schema_enforced_and_redaction(store):
    diag.record("chart_refresh", symbol="AAA", ok=True, bogus="x", api_key="SECRET")
    rec = diag.load_day(datetime.now(tz=ET).date())[0]
    assert "bogus" not in rec                      # unknown field dropped
    assert "api_key" not in rec                    # sensitive key dropped
    assert rec["ts_utc"].endswith("+00:00")


def test_concurrent_writes_no_interleave(store):
    errs = []
    def w(i):
        try:
            for j in range(50):
                diag.record("alert_eval", alerts_evaluated=j, busy=False)
        except Exception as e:  # noqa
            errs.append(e)
    threads = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errs
    recs = diag.load_day(datetime.now(tz=ET).date())
    assert len(recs) == 400
    assert all(r["kind"] == "alert_eval" for r in recs)


def test_daily_file_partition(store):
    today = datetime.now(tz=ET).date()
    diag.record("scanner_eval", session="regular")
    assert (store / f"{today.isoformat()}.jsonl").exists()


def test_corrupt_lines_skipped(store):
    p = store / f"{DAY.isoformat()}.jsonl"
    p.write_text('{"kind":"scanner_eval","ts_utc":"x"}\nNOT_JSON\n{"kind":"alert_eval","ts_utc":"y"}\n')
    recs = diag.load_day(DAY)
    assert len(recs) == 2


def test_retention_prunes_old_files(store):
    old = datetime.now(tz=ET).date() - timedelta(days=40)
    (store / f"{old.isoformat()}.jsonl").write_text("{}\n")
    diag.record("scanner_eval")
    assert not (store / f"{old.isoformat()}.jsonl").exists()


def test_size_limit_stops_appending(store, monkeypatch):
    monkeypatch.setattr(diag, "MAX_FILE_BYTES", 10)
    diag.record("scanner_eval", session="regular")
    diag.record("scanner_eval", session="regular")
    recs = diag.load_day(datetime.now(tz=ET).date())
    assert len(recs) <= 1


def test_record_never_raises(store, monkeypatch):
    monkeypatch.setattr(diag, "_append", lambda *a: 1/0)
    diag.record("scanner_eval")  # must swallow, not propagate


def test_unknown_kind_ignored(store):
    diag.record("not_a_kind", whatever=1)
    assert diag.load_day(datetime.now(tz=ET).date()) == []


def test_report_aggregation(store):
    today = datetime.now(tz=ET).date()
    path = store / f"{today.isoformat()}.jsonl"
    recs = [
        {"ts_utc": "2026-01-15T14:00:00+00:00", "kind": "chart_refresh", "ok": True, "data_age_ms": 52000, "fetch_ms": 500},
        {"ts_utc": "2026-01-15T14:00:20+00:00", "kind": "chart_refresh", "ok": False, "error": "timeout"},
        {"ts_utc": "2026-01-15T14:00:20+00:00", "kind": "alert_eval", "alerts_evaluated": 3, "skipped_symbols": ["ZZZ"]},
        {"ts_utc": "2026-01-15T14:00:21+00:00", "kind": "alert_triggered", "alert_latency_ms": 21000},
        {"ts_utc": "2026-01-15T14:01:00+00:00", "kind": "client_event", "client_kind": "notification_attempt"},
        {"ts_utc": "2026-01-15T14:02:00+00:00", "kind": "sim_run", "trades_entered": 2, "unresolved": 1, "net_pnl": 3.84},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    rep = diag.session_report(today, None)
    assert rep["chart_refreshes"]["ok"] == 1 and rep["chart_refreshes"]["failed"] == 1
    assert rep["data_freshness_ms"]["median"] == 52000
    assert rep["alert_evaluations"]["skipped_symbols"] == ["ZZZ"]
    assert rep["alert_events"]["alert_latency_ms"]["median"] == 21000
    assert rep["client_reported"]["count"] == 1
    assert "browser" in rep["client_reported"]["note"]
    assert rep["simulations"]["trades"] == 2 and rep["simulations"]["unresolved"] == 1
    assert rep["calendar"]["verified"] == "unavailable"   # honest, not zero-errors


def test_no_observation_session(store):
    rep = diag.session_report(DAY, None)
    assert rep["observation_window"]["available"] is False
    assert rep["data_freshness_ms"]["available"] is False


def test_completeness_flags_partial_session(store):
    """Observed window far shorter than session hours -> partial flagged."""
    today = datetime.now(tz=ET).date()
    path = store / f"{today.isoformat()}.jsonl"
    path.write_text(json.dumps({"ts_utc": "2026-01-15T15:00:00+00:00", "kind": "alert_eval"}) + "\n" +
                    json.dumps({"ts_utc": "2026-01-15T15:10:00+00:00", "kind": "alert_eval"}) + "\n")
    from datetime import time as dtime
    rep = diag.session_report(today, (dtime(9, 30), dtime(16, 0)))
    assert rep["completeness"]["partial"] is True
    assert rep["completeness"]["coverage_fraction_estimated"] < 0.1


def test_size_cap_reported_as_limitation(store, monkeypatch):
    """Truncated files must surface a completeness note, not silent totals."""
    monkeypatch.setattr(diag, "MAX_FILE_BYTES", 5)
    diag.record("scanner_eval", session="regular")
    rep = diag.session_report(datetime.now(tz=ET).date(), None)
    assert any("size cap" in n for n in rep["completeness"]["notes"])


def test_raw_vs_unique_and_replay(store):
    """40 logged triggers for one real event -> raw 40, unique 1, replay excluded from latency."""
    today = datetime.now(tz=ET).date()
    path = store / f"{today.isoformat()}.jsonl"
    lines = [
        json.dumps({"kind": "alert_triggered", "alert_id": "a1", "bar_ts": "t1",
                    "alert_latency_ms": 15_000_000, "replay": True, "ts_utc": "x"})
        for _ in range(40)
    ]
    lines.append(json.dumps({"kind": "alert_triggered", "alert_id": "a1",
                             "bar_ts": "t2", "alert_latency_ms": 21000,
                             "replay": False, "ts_utc": "y"}))
    path.write_text("".join(l + "\n" for l in lines))
    rep = diag.session_report(today, None)
    ev = rep["alert_events"]
    assert ev["raw_records"] == 41
    assert ev["unique_events"] == 2
    assert ev["replay_events"] == 40
    assert ev["alert_latency_ms"]["median"] == 21000   # replays excluded


def test_freshness_split_by_session(store):
    today = datetime.now(tz=ET).date()
    path = store / f"{today.isoformat()}.jsonl"
    recs = [
        {"kind": "chart_refresh", "ok": True, "data_age_ms": 1000, "session": "regular", "ts_utc": "a"},
        {"kind": "chart_refresh", "ok": True, "data_age_ms": 9_000_000, "session": "closed", "ts_utc": "b"},
        {"kind": "chart_refresh", "ok": True, "data_age_ms": 500, "ts_utc": "c"},  # legacy record
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    rep = diag.session_report(today, None)
    assert rep["data_freshness_rth_ms"]["median"] == 1000
    assert rep["data_freshness_non_rth_ms"]["median"] == 9_000_000
    assert rep["data_freshness_ms"]["count"] == 3      # legacy included in overall


def test_api_error_schema(store):
    diag.record("api_error", context="alerts_evaluate", error="NameError: t_eval")
    rec = diag.load_day(datetime.now(tz=ET).date())[0]
    assert rec["kind"] == "api_error" and "t_eval" in rec["error"]
    diag.record("api_error", context="x", api_key="S")   # redaction still applies
    recs = diag.load_day(datetime.now(tz=ET).date())
    assert all("api_key" not in r for r in recs)
