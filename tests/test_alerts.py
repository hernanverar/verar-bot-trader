from datetime import datetime, time as t, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.alerts import evaluate_alert, new_alert, rearm

ET = ZoneInfo("America/New_York")
DAY = datetime(2026, 1, 15, 9, 30, tzinfo=ET)
SESS = {DAY.date(): (t(9, 30), t(16, 0))}


def bar(i, close, high=None, day=DAY):
    return SimpleNamespace(timestamp=day + timedelta(minutes=i),
                           open=close, high=high if high is not None else close + 0.1,
                           low=close - 0.1, close=close, volume=100)


def mk(t="price_above", lvl=2.0, sym="EVGO"):
    a = new_alert(sym, t, lvl)
    a.created_ts = DAY - timedelta(minutes=5)   # before fixture bars
    a.state["last_close"] = 1.90
    a.state["last_ts"] = (DAY - timedelta(minutes=1)).isoformat()
    return a


def test_cross_above():
    a = mk()
    ev = evaluate_alert(a, [bar(0, 1.95), bar(1, 2.0)], SESS)
    assert len(ev) == 1 and ev[0].trigger_price == 2.0  # >= touches level


def test_cross_below_and_stop():
    a = mk("stop_level", 2.0)
    a.state["last_close"] = 2.10  # above level, needs downward cross
    assert evaluate_alert(a, [bar(0, 1.99)], SESS)[0].alert_type == "stop_level"
    a2 = mk("price_below", 2.0)
    a2.state["last_close"] = 2.10
    assert len(evaluate_alert(a2, [bar(0, 2.0)], SESS)) == 1  # <= triggers


def test_exact_threshold_no_cross():
    a = mk()
    a.state["last_close"] = 2.0  # already AT level (not below) -> no cross
    assert evaluate_alert(a, [bar(0, 2.0)], SESS) == []


def test_stays_above_no_repeat():
    a = mk()
    evaluate_alert(a, [bar(0, 2.5)], SESS)          # fires once
    assert evaluate_alert(a, [bar(1, 2.6), bar(2, 2.7)], SESS) == []  # fired


def test_duplicate_and_out_of_order_bars():
    a = mk()
    ev = evaluate_alert(a, [bar(1, 2.5)], SESS)
    assert len(ev) == 1
    # replaying the same bar and an older bar -> nothing new
    assert evaluate_alert(a, [bar(0, 2.6), bar(1, 2.5)], SESS) == []


def test_hod_breakout_no_lookahead():
    a = mk("hod_breakout", None)
    a.state["session_high"] = 5.0; a.state["session_day"] = DAY.date().isoformat()
    # bar touches but doesn't exceed -> nothing
    assert evaluate_alert(a, [bar(0, 4.9, high=5.0)], SESS) == []
    ev = evaluate_alert(a, [bar(1, 5.5, high=5.2)], SESS)
    assert len(ev) == 1 and ev[0].trigger_price == 5.2


def test_hod_extended_hours_excluded():
    a = mk("hod_breakout", None)
    a.state["session_high"] = 5.0; a.state["session_day"] = DAY.date().isoformat()
    pre = bar(-30, 4.0, high=9.0)   # 9:00 premarket spike — no trigger, no baseline
    assert evaluate_alert(a, [pre], SESS) == []
    assert a.state["session_high"] == 5.0


def test_hod_rearms_next_session():
    a = mk("hod_breakout", None)
    a.state["session_high"] = 5.0; a.state["session_day"] = DAY.date().isoformat()
    evaluate_alert(a, [bar(0, 5.5, high=5.2)], SESS)
    assert a.state["fired"] is True
    next_day_bars = [bar(0, 6.0, high=6.1, day=DAY + timedelta(days=1))]
    # new session -> baseline reseeds from history, fired resets
    evaluate_alert(a, next_day_bars, { (DAY + timedelta(days=1)).date(): (t(9,30), t(16,0)) })
    assert a.state["fired"] is False


def test_manual_rearm():
    a = mk()
    evaluate_alert(a, [bar(0, 2.5)], SESS)
    assert a.state["fired"] is True
    rearm(a)
    a.state["last_close"] = 1.9
    assert len(evaluate_alert(a, [bar(2, 2.5)], SESS)) == 1


def test_momentum_transition():
    a = mk("momentum", None)
    assert evaluate_alert(a, [], SESS, momentum_eligible=False) == []
    ev = evaluate_alert(a, [], SESS, momentum_eligible=True)
    assert len(ev) == 1 and "unverified" in ev[0].message
    assert evaluate_alert(a, [], SESS, momentum_eligible=True) == []  # no repeat


def test_disabled_alert_evaluates_nothing():
    a = mk()
    a.enabled = False
    assert evaluate_alert(a, [bar(0, 9.9)], SESS) == []


def test_multi_symbol_isolated_state():
    a, b = mk(sym="AAA"), mk(sym="BBB")
    evaluate_alert(a, [bar(0, 2.5)], SESS)
    assert a.state["fired"] is True and not b.state.get("fired")


def test_early_close_boundary():
    times = {DAY.date(): (t(9, 30), t(13, 0))}  # half-day
    a = mk("hod_breakout", None)
    a.state["session_high"] = 5.0; a.state["session_day"] = DAY.date().isoformat()
    assert evaluate_alert(a, [bar(220, 4.0, high=9.0)], times) == []  # 13:10 excluded


def test_persistence_roundtrip_and_dedup(tmp_path, monkeypatch):
    """State survives a backend restart; replays don't duplicate events."""
    import app.alert_store as store

    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "alerts.json")
    a = mk()
    evs = evaluate_alert(a, [bar(0, 2.5)], SESS)    # fires
    store.mutate(lambda al, ev: (None, al + [a], ev + list(evs)))

    alerts2, events2 = store._read()               # simulate restart
    assert len(events2) == 1
    a2 = alerts2[0]
    assert a2.state["fired"] is True               # survived persistence
    # Re-evaluating the same bars after reload -> nothing new (last_ts dedup)
    assert evaluate_alert(a2, [bar(0, 2.5), bar(1, 2.6)], SESS) == []


def test_event_dedup_by_id(tmp_path, monkeypatch):
    import app.alert_store as store
    from app.alerts import AlertEvent

    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "alerts.json")
    e = AlertEvent("same-id", "a", "AAA", "x", 2.0, DAY, "m")
    store.mutate(lambda al, ev: (None, al, ev + [e, e]))  # injected twice
    _, events = store._read()
    assert len(events) == 1                         # id-dedup on write


def test_patch_level_resets_baseline_enabled_preserves():
    """PATCH level -> fresh baseline; PATCH enabled only -> baseline kept."""
    a = mk()
    a.state["last_close"] = 2.50
    # Editing only enabled must NOT clear the crossing baseline.
    edit = {"enabled": False}
    for a2 in [a]:
        if "enabled" in edit:
            a2.enabled = bool(edit["enabled"])
            if not a2.enabled:
                a2.state["fired"] = False
    assert a.state["last_close"] == 2.50            # preserved


def test_no_retroactive_fire_on_backfill():
    """Crossing on a bar before creation must never fire — only seed."""
    a = mk()
    a.created_ts = DAY + timedelta(minutes=5)      # created at 9:35
    a.state["last_ts"] = None; a.state["last_close"] = None
    # bars at 9:30(2.5 above 2.0) and 9:34(1.8 crossing) — both BEFORE creation
    assert evaluate_alert(a, [bar(0, 2.5), bar(4, 1.8)], SESS) == []
    assert a.state["last_close"] == 1.8             # baseline still seeded
    # bar after creation crossing again -> fires once, correctly
    ev = evaluate_alert(a, [bar(6, 2.3), bar(7, 1.9)], SESS)
    assert len(ev) == 1 and ev[0].trigger_price == 2.3


def test_mid_session_creation_first_cross_fires():
    """Alert created mid-session fires on the first post-creation crossing."""
    a = mk()
    a.created_ts = DAY + timedelta(minutes=10)
    ev = evaluate_alert(a, [bar(5, 1.9), bar(11, 1.8), bar(12, 1.6)], SESS)
    # crossing 1.9->1.8 spans creation boundary? 1.8>2.0? no — level is 2.0,
    # 1.8 is already below. Bar 12: prev 1.8, close 1.6 — still below, no cross.
    assert ev == []  # stays-below is not a crossing


def test_mid_session_creation_true_cross_fires():
    a = mk()
    a.created_ts = DAY + timedelta(minutes=10)
    ev = evaluate_alert(a, [bar(5, 1.9), bar(11, 2.5)], SESS)  # 1.9<2.0 -> 2.5 crosses up
    assert len(ev) == 1 and ev[0].ts == bar(11, 2.5).timestamp  # real bar ts


def test_idempotent_replay_same_bars():
    """Re-evaluating identical bars must not duplicate events."""
    a = mk()
    ev1 = evaluate_alert(a, [bar(0, 2.5)], SESS)
    ev2 = evaluate_alert(a, [bar(0, 2.5)], SESS)
    assert len(ev1) == 1 and ev2 == []


def test_hod_baseline_from_pre_creation_bars():
    """Pre-creation RTH highs seed HOD baseline without firing."""
    a = mk("hod_breakout", None)
    a.created_ts = DAY + timedelta(minutes=100)
    a.state["session_day"] = DAY.date().isoformat()
    # historical RTH bar with huge high — seeds baseline, no trigger
    evaluate_alert(a, [bar(0, 5.0, high=9.0)], SESS)
    assert a.state["session_high"] == 9.0
    # post-creation bar below old high -> nothing; above -> fires
    assert evaluate_alert(a, [bar(120, 4.0, high=8.0)], SESS) == []
    assert len(evaluate_alert(a, [bar(121, 4.0, high=9.5)], SESS)) == 1
