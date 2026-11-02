"""Configurable alert engine — informational only, completed-bar based.

Evaluation is deterministic and offline-testable: the engine consumes
lists of *completed* bars plus session context and produces events.
Crossing semantics are directional:
  - above: prev < level and current >= level
  - below/stop: prev > level and current <= level
Alerts fire once per arming; HOD breakouts once per trading session.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Literal
from uuid import uuid4

from app.market_data import MARKET_TZ

AlertType = Literal["price_above", "price_below", "hod_breakout", "momentum", "stop_level"]
VALID_TYPES = {"price_above", "price_below", "hod_breakout", "momentum", "stop_level"}


@dataclass
class Alert:
    id: str
    symbol: str
    alert_type: AlertType
    level: float | None  # price_above / price_below / stop_level
    enabled: bool
    created_ts: datetime
    # Engine state — not user-facing config.
    state: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        state = dict(self.state)
        for k in ("last_ts",):
            if isinstance(state.get(k), datetime):
                state[k] = state[k].isoformat()
        if isinstance(state.get("session_day"), date) and not isinstance(state.get("session_day"), str):
            state["session_day"] = state["session_day"].isoformat()
        return {
            "id": self.id,
            "symbol": self.symbol,
            "alert_type": self.alert_type,
            "level": self.level,
            "enabled": self.enabled,
            "created_ts": self.created_ts.isoformat(),
            "state": state,
        }

    @staticmethod
    def from_dict(d: dict) -> "Alert":
        return Alert(
            id=d["id"],
            symbol=d["symbol"],
            alert_type=d["alert_type"],
            level=d.get("level"),
            enabled=bool(d.get("enabled", True)),
            created_ts=datetime.fromisoformat(d["created_ts"]),
            state=dict(d.get("state") or {}),
        )


@dataclass
class AlertEvent:
    id: str
    alert_id: str
    symbol: str
    alert_type: str
    trigger_price: float
    ts: datetime
    message: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "alert_id": self.alert_id,
            "symbol": self.symbol,
            "alert_type": self.alert_type,
            "trigger_price": self.trigger_price,
            "ts": self.ts.isoformat(),
            "message": self.message,
        }

    @staticmethod
    def from_dict(d: dict) -> "AlertEvent":
        return AlertEvent(
            id=d["id"],
            alert_id=d["alert_id"],
            symbol=d["symbol"],
            alert_type=d["alert_type"],
            trigger_price=float(d["trigger_price"]),
            ts=datetime.fromisoformat(d["ts"]),
            message=d["message"],
        )


def new_alert(symbol: str, alert_type: str, level: float | None = None) -> Alert:
    return Alert(
        id=str(uuid4()),
        symbol=symbol.upper(),
        alert_type=alert_type,  # type: ignore[assignment]
        level=level,
        enabled=True,
        created_ts=datetime.now(tz=MARKET_TZ),
    )


def _in_rth(ts: datetime, session_times: dict | None) -> bool:
    t = ts.astimezone(MARKET_TZ)
    times = (session_times or {}).get(t.date())
    lo, hi = times if times else (dtime(9, 30), dtime(16, 0))
    return lo <= t.time() < hi


def evaluate_alert(
    alert: Alert,
    bars: list,
    session_times: dict | None = None,
    momentum_eligible: bool | None = None,
) -> list[AlertEvent]:
    """Evaluate one alert against newly completed bars (chronological).

    `bars` must be completed bars; bars at/before the alert's last seen
    timestamp are skipped (dedup, out-of-order protection). `state`
    fields: last_ts, last_close, session_day, session_high, fired.
    `momentum_eligible` is the VERAR numeric classification for the
    current snapshot (float/news always unverified upstream).
    """
    events: list[AlertEvent] = []
    st = alert.state
    if not alert.enabled:
        return events

    st.setdefault("last_ts", None)
    st.setdefault("last_close", None)
    st.setdefault("fired", False)
    st.setdefault("session_day", None)
    st.setdefault("session_high", None)
    st.setdefault("prev_eligible", None)

    last_ts = st["last_ts"]
    if isinstance(last_ts, str):
        last_ts = datetime.fromisoformat(last_ts)  # persisted state
    if isinstance(st.get("session_day"), str):
        st["session_day"] = date.fromisoformat(st["session_day"])

    for b in sorted(bars, key=lambda x: x.timestamp):
        ts = b.timestamp
        if last_ts is not None and ts <= last_ts:
            continue  # already seen / out of order
        day = ts.astimezone(MARKET_TZ).date()

        if alert.alert_type == "hod_breakout":
            if not _in_rth(ts, session_times):
                continue  # extended-hours bars never form the baseline
            if st["session_day"] != day:
                # New session: seed baseline from history silently; the
                # fired flag resets once per trading day.
                st["session_day"] = day
                if st["session_high"] is None:
                    st["session_high"] = b.high
                    st["last_close"], st["last_ts"] = b.close, ts
                    continue
                st["session_high"] = None
                st["fired"] = False
            high = st["session_high"]
            if high is not None and b.high > high and not st["fired"]:
                st["fired"] = True
                events.append(AlertEvent(
                    id=str(uuid4()), alert_id=alert.id, symbol=alert.symbol,
                    alert_type=alert.alert_type, trigger_price=round(b.high, 4),
                    ts=ts,
                    message=f"{alert.symbol} broke session high {high:.2f} -> {b.high:.2f} (RTH)",
                ))
            st["session_high"] = max(high or 0.0, b.high)

        elif alert.alert_type in ("price_above", "price_below", "stop_level"):
            if st["fired"]:
                st["last_close"], st["last_ts"] = b.close, ts
                continue
            prev = st["last_close"]
            lvl = alert.level
            if prev is not None and lvl is not None:
                above = alert.alert_type == "price_above" and prev < lvl <= b.close
                below = (
                    alert.alert_type in ("price_below", "stop_level")
                    and prev > lvl >= b.close
                )
                if above or below:
                    st["fired"] = True
                    direction = "above" if above else "below"
                    events.append(AlertEvent(
                        id=str(uuid4()), alert_id=alert.id, symbol=alert.symbol,
                        alert_type=alert.alert_type,
                        trigger_price=round(b.close, 4), ts=ts,
                        message=(
                            f"{alert.symbol} crossed {direction} ${lvl:.2f} "
                            f"at ${b.close:.2f}"
                            + (" (stop level)" if alert.alert_type == "stop_level" else "")
                        ),
                    ))

        st["last_close"], st["last_ts"] = b.close, ts

    if alert.alert_type == "momentum" and momentum_eligible is not None:
        prev = st["prev_eligible"]
        st["prev_eligible"] = momentum_eligible
        if momentum_eligible and prev is False and not st["fired"]:
            st["fired"] = True
            events.append(AlertEvent(
                id=str(uuid4()), alert_id=alert.id, symbol=alert.symbol,
                alert_type="momentum", trigger_price=0.0,
                ts=datetime.now(tz=MARKET_TZ),
                message=(
                    f"{alert.symbol} qualifies as VERAR numeric momentum "
                    "candidate (float/news unverified, IEX volume)"
                ),
            ))
    return events


def rearm(alert: Alert) -> None:
    """Manual rearm: clears the fired flag without touching baselines."""
    alert.state["fired"] = False
