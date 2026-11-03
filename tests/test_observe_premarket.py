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
    assert s["qualified_count"] == 1 and s["first_source_counts"]["gap_scan"] == 1
    assert s["observations"][0]["qualified"] == 1


def test_failed_observation_recorded():
    recs = [{"scheduled": "t", "requested_ts": "x", "status": None,
             "error": "ConnectionError", "response": None}]
    s = summarize({}, recs)
    assert len(s["errors"]) == 1 and s["observations"][0]["rows"] == 0
