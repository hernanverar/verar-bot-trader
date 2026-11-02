"""Alert persistence — JSON file with atomic writes and a process lock.

Local single-backend prototype storage: data/alerts.json. Not suitable
for multi-instance deployments; reads tolerate missing/corrupt files.
"""

import json
import os
import threading
from pathlib import Path

from app.alerts import Alert, AlertEvent

STORE_PATH = Path(__file__).resolve().parent.parent / "data" / "alerts.json"
_lock = threading.Lock()
MAX_EVENTS = 500


def _read() -> tuple[list[Alert], list[AlertEvent]]:
    if not STORE_PATH.exists():
        return [], []
    try:
        raw = json.loads(STORE_PATH.read_text())
        alerts = [Alert.from_dict(d) for d in raw.get("alerts", [])]
        events = [AlertEvent.from_dict(d) for d in raw.get("events", [])]
        return alerts, events
    except (json.JSONDecodeError, KeyError, ValueError):
        return [], []


def _write(alerts: list[Alert], events: list[AlertEvent]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    # Dedup by event id — a race can never double-record the same trigger.
    seen: set[str] = set()
    unique = [e for e in events if e.id not in seen and not seen.add(e.id)]
    payload = {
        "alerts": [a.to_dict() for a in alerts],
        "events": [e.to_dict() for e in unique[-MAX_EVENTS:]],
    }
    tmp.write_text(json.dumps(payload, indent=1))
    os.replace(tmp, STORE_PATH)  # atomic on POSIX and Windows


def load_alerts() -> list[Alert]:
    with _lock:
        return _read()[0]


def load_events() -> list[AlertEvent]:
    with _lock:
        return _read()[1]


def mutate(fn):
    """Apply `fn(alerts, events) -> result` under lock, then persist."""
    with _lock:
        alerts, events = _read()
        result, alerts, events = fn(alerts, events)
        _write(alerts, events)
        return result
