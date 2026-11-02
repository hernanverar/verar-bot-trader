"""Historical momentum research snapshots — immutable, versioned, hashed.

Stores 1m bar snapshots + acquisition metadata under data/research/
(gitignored). A T+1 reacquisition creates a NEW version — never mutates
prior data. Canonical result comparison excludes run-specific metadata
(acquired_ts, request counts) so identical input+config+implementation
reproduces identical canonical output.
"""

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent / "data" / "research"
_LOCK = threading.Lock()


# ---- Screening --------------------------------------------------------

@dataclass(frozen=True)
class ResearchScreenConfig:
    gain_min_pct: float = 10.0        # ross_numeric_candidate tier
    price_min: float = 1.0
    price_max: float = 10.0
    rvol_min: float = 5.0
    approx_gain_min_pct: float = 7.0  # research_approx tier
    approx_price_max: float = 15.0
    approx_rvol_min: float = 2.0
    rvol_window: int = 10             # prior-day baseline bars
    min_history: int = 10


@dataclass(frozen=True)
class ScreenedCandidate:
    symbol: str
    session_date: str
    gain_pct: float
    close: float
    rvol: float
    tier: str            # ross_numeric_candidate | research_approx | nonqualifier
    float_verified: str = "unverified"
    catalyst_verified: str = "unverified"


def screen_daily_bars(
    daily: dict[str, list], config: ResearchScreenConfig | None = None
) -> list[ScreenedCandidate]:
    """Post-session daily screening — point-in-time vs prior bars only.

    NOTE: end-of-day gain/volume was NOT known intraday — this classifies
    sessions, never intraday eligibility. All candidates recorded
    (including nonqualifiers) to reduce selection bias.
    """
    cfg = config or ResearchScreenConfig()
    out: list[ScreenedCandidate] = []
    for sym in sorted(daily):
        bars = daily[sym]
        for i in range(cfg.min_history, len(bars)):
            b, prev = bars[i], bars[i - 1]
            if not prev.close:
                continue
            gain = (b.close / prev.close - 1) * 100
            prior = [x.volume for x in bars[max(0, i - cfg.rvol_window):i] if x.volume]
            base = median(prior) if prior else 0.0
            rvol = (b.volume / base) if base else 0.0
            d = b.timestamp.date().isoformat()
            if (gain >= cfg.gain_min_pct and cfg.price_min <= b.close <= cfg.price_max
                    and rvol >= cfg.rvol_min):
                tier = "ross_numeric_candidate"
            elif (gain >= cfg.approx_gain_min_pct and b.close <= cfg.approx_price_max
                    and rvol >= cfg.approx_rvol_min and b.close >= cfg.price_min):
                tier = "research_approx"
            else:
                tier = "nonqualifier"
            out.append(ScreenedCandidate(
                symbol=sym, session_date=d, gain_pct=round(gain, 2),
                close=round(b.close, 4), rvol=round(rvol, 2), tier=tier))
    return out


# ---- Coverage / quality ------------------------------------------------

def coverage_stats(
    bars: list, expected_minutes: int, gap_threshold_min: int = 3
) -> dict:
    """Unique-minute coverage + gap stats. expected_minutes must come
    from a VERIFIED calendar (390 full, 210 early close)."""
    ts = sorted({b.timestamp for b in bars})
    gaps = []
    for a, b in zip(ts, ts[1:]):
        m = (b - a).total_seconds() / 60 - 1
        if m >= gap_threshold_min:
            gaps.append({"after": a.isoformat(), "minutes": int(m)})
    pct = round(100 * len(ts) / expected_minutes, 1) if expected_minutes else 0.0
    return {
        "observed_minutes": len(ts),
        "expected_minutes": expected_minutes,
        "coverage_pct": pct,
        "gaps_over_3m": len(gaps),
        "gaps": gaps[:50],
        "confidence": "low" if pct < 80 else "ok",
        "note": "missing IEX minutes are unobserved, not proven zero-trade",
    }


# ---- Snapshot storage --------------------------------------------------

def _snap_dir(symbol: str, session: str, feed: str = "iex") -> Path:
    # iex keeps the legacy path for backward compatibility with existing
    # snapshots and result references; other feeds live in a subdir.
    base = ROOT / "snapshots" / symbol.upper() / session
    return base if feed == "iex" else base / feed


def _next_version(d: Path) -> int:
    vs = [int(p.stem[1:]) for p in d.glob("v*.jsonl") if p.stem[1:].isdigit()]
    return max(vs, default=0) + 1


def _atomic_write(path: Path, payload: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_snapshot(
    symbol: str, session: str, bars: list, meta: dict, feed: str = "iex"
) -> dict:
    """Write an immutable versioned snapshot. Returns meta with sha256."""
    meta = {**meta, "feed": feed}
    d = _snap_dir(symbol, session, feed)
    v = _next_version(d)
    d.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(
        json.dumps({"t": b.timestamp.isoformat(), "o": b.open, "h": b.high,
                    "l": b.low, "c": b.close, "v": b.volume})
        for b in bars
    )
    sha = hashlib.sha256(payload.encode()).hexdigest()
    with _LOCK:
        _atomic_write(d / f"v{v}.jsonl", payload + "\n")
        meta = {**meta, "version": v, "sha256": sha,
                "acquired_ts": datetime.now().astimezone().isoformat(),
                "bars": len(bars)}
        _atomic_write(d / f"meta_v{v}.json", json.dumps(meta, indent=2))
    return meta


def list_versions(symbol: str, session: str, feed: str = "iex") -> list[int]:
    d = _snap_dir(symbol, session, feed)
    return sorted(int(p.stem[1:]) for p in d.glob("v*.jsonl")
                  if p.stem[1:].isdigit())


def load_snapshot(symbol: str, session: str, version: int | None = None,
                  feed: str = "iex") -> list[dict]:
    d = _snap_dir(symbol, session, feed)
    if version is None:
        vs = list_versions(symbol, session)
        if not vs:
            return []
        version = vs[-1]
    path = d / f"v{version}.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def snapshot_hash(symbol: str, session: str, version: int, feed: str = "iex") -> str | None:
    meta = _snap_dir(symbol, session, feed) / f"meta_v{version}.json"
    if not meta.exists():
        return None
    return json.loads(meta.read_text()).get("sha256")


def compare_versions(symbol: str, session: str, v1: int, v2: int,
                     feed: str = "iex") -> dict:
    """Diff two frozen snapshots — detects backfill/changed bars."""
    a = {r["t"]: r for r in load_snapshot(symbol, session, v1, feed)}
    b = {r["t"]: r for r in load_snapshot(symbol, session, v2, feed)}
    changed = [t for t in a if t in b and a[t] != b[t]]
    return {
        "added": sorted(t for t in b if t not in a),
        "removed": sorted(t for t in a if t not in b),
        "changed": sorted(changed),
        "identical": not changed and len(a) == len(b),
    }


# ---- Canonical results -------------------------------------------------

def config_hash(cfg) -> str:
    """SHA-256 of the detector config's canonical field dump."""
    return hashlib.sha256(
        json.dumps(asdict(cfg), sort_keys=True).encode()).hexdigest()[:16]


def canonical_result(result: dict) -> str:
    """Canonical hash — strips run-specific fields so identical
    input+config+implementation compares equal."""
    skip = {"run_ts", "acquired_ts", "git_commit", "snapshot_version"}
    body = {k: v for k, v in sorted(result.items()) if k not in skip}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def save_result(symbol: str, session: str, version: int, result: dict,
                feed: str = "iex") -> Path:
    d = ROOT / "results" / symbol.upper() / session
    if feed != "iex":
        d = d / feed
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"v{version}.json"
    with _LOCK:
        _atomic_write(path, json.dumps(result, indent=2))
    return path


def save_screening(name: str, candidates: list[ScreenedCandidate],
                   config: ResearchScreenConfig) -> Path:
    d = ROOT / "candidates"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"screen_{name}.json"
    body = {"config": asdict(config),
            "candidates": [asdict(c) for c in candidates]}
    with _LOCK:
        _atomic_write(path, json.dumps(body, indent=2))
    return path
