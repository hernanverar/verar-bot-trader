import sys
sys.path.insert(0, "scripts")
from observe_premarket import due_slots, update_history, summarize


def test_due_slots():
    from datetime import date
    slots = due_slots(date(2026, 11, 3))
    assert len(slots) == 5 and slots[0].hour == 7 and slots[-1].hour == 9
    assert slots[0].tzinfo is not None


def test_history_tracks_first_seen_vs_qualified():
    h = {}
    update_history(h, [{"symbol": "X", "source": "gap_scan", "qualified": False,
                        "status": "stale", "last": 2.0}], "07:00")
    update_history(h, [{"symbol": "X", "source": "most_actives", "qualified": True,
                        "gap_pct": 12.0, "status": "ok"}], "08:00")
    assert h["X"]["first_seen"] == "07:00" and h["X"]["first_source"] == "gap_scan"
    assert h["X"]["first_qualified_ts"] == "08:00" and h["X"]["first_gap"] == 12.0
    assert h["X"]["sources"] == ["gap_scan", "most_actives"]


def test_summarize_counts():
    h = {}
    update_history(h, [{"symbol": "A", "source": "gap_scan", "qualified": True,
                        "gap_pct": 15.0}], "07:00")
    recs = [{"scheduled": "t", "requested_ts": "07:00", "status": 200,
             "response": {"market": "premarket", "scan": {"candidates": 3},
                          "rows": [{"source": "gap_scan", "qualified": True,
                                    "status": "ok", "rvol": 5.0}]}}]
    s = summarize(h, recs)
    assert s["qualified_total"] == 1 and s["first_source_counts"]["gap_scan"] == 1
    assert s["observations"][0]["qualified"] == 1


def test_failed_observation_recorded():
    recs = [{"scheduled": "t", "requested_ts": "x", "status": None,
             "error": "ConnectionError", "response": None}]
    s = summarize({}, recs)
    assert len(s["errors"]) == 1 and s["observations"][0]["rows"] == 0


def _rec(missed, market="premarket", rows=None, status=200):
    return {"scheduled": "t", "requested_ts": "x", "status": status,
            "missed": missed, "error": None,
            "response": {"market": market, "rows": rows or []}}


def test_all_slots_missed_not_validated():
    recs = [_rec(True) for _ in range(5)]
    s = summarize({}, recs)
    assert s["session_validity"] == "not_validated"
    assert s["valid_observation_slots"] == 0 and s["missed_observation_slots"] == 5


def test_mixed_valid_and_missed():
    recs = [_rec(False), _rec(False), _rec(True), _rec(True), _rec(True)]
    s = summarize({}, recs)
    assert s["session_validity"] == "partial"
    assert s["valid_observation_slots"] == 2 and s["missed_observation_slots"] == 3


def test_qualified_fresh_vs_stale_split():
    rows = [{"qualified": True, "status": "ok"},
            {"qualified": True, "status": "stale"},
            {"qualified": False, "status": "ok"}]
    s = summarize({}, [_rec(False, rows=rows)])
    assert s["qualified_fresh"] == 1 and s["qualified_stale"] == 1


def test_first_detected_vs_market_observation():
    h = {}
    update_history(h, [{"symbol": "X", "source": "gap_scan",
                        "observed_ts": "2026-11-03T13:30:00+00:00",
                        "qualified": True, "gap_pct": 12.0, "status": "stale"}],
                   "2026-11-03T17:49:00-05:00")
    assert h["X"]["first_detected_at"] == "2026-11-03T17:49:00-05:00"
    assert h["X"]["observations"][0]["observed_ts"] == "2026-11-03T13:30:00+00:00"


def test_missed_slot_qualified_is_contextual_only():
    # qualified+fresh row inside a MISSED capture must never be prospective
    rows = [{"qualified": True, "status": "ok"}]
    s = summarize({}, [_rec(True, rows=rows)])
    assert s["qualified_fresh"] == 0 and s["qualified_contextual"] == 1


def test_out_of_window_qualified_is_contextual():
    rows = [{"qualified": True, "status": "ok"}]
    s = summarize({}, [_rec(False, market="premarket_ended", rows=rows)])
    assert s["qualified_fresh"] == 0 and s["qualified_contextual"] == 1
