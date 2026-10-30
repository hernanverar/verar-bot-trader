"""Intraday hot-stock scanner. Read-only — never submits orders.

Ranks symbols on: intraday % change vs prior close, relative volume vs a
same-time-of-day baseline, dollar volume, short-window momentum, and quoted
spread when available.

Scores are a transparent weighted heuristic for ranking attention, NOT a
validated predictor of future returns.

RVOL methodology: today's cumulative volume is compared against the average
cumulative volume observed at the same time of day over the trailing
`baseline_sessions` sessions. This is a defensible baseline (unlike comparing
to recent 1-minute bars), but it inherits the configured data feed's coverage:
on the free IEX feed, volumes reflect only IEX executions and understate
consolidated tape volume for all symbols roughly uniformly.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.intraday_data import (
    bars_by_session,
    completed_minute_bars,
    fetch_minute_bars,
    session_cum_volume_at,
)
from app.market_data import (
    MARKET_TZ,
    fetch_latest_quotes,
    get_data_client,
    spread_bps_from_quote,
)

LOOKBACK_CALENDAR_DAYS = 15  # covers weekends/holidays around ~7 sessions


@dataclass(frozen=True)
class ScannerConfig:
    min_price: float = 1.0
    min_dollar_volume: float = 1_000_000.0
    min_rvol: float | None = None  # optional gate, e.g. 1.5
    min_positive_change_pct: float = 3.0  # percent units; +3.0 = +3%
    require_fresh_data: bool = True
    baseline_sessions: int = 5
    min_baseline_sessions: int = 2
    momentum_window_min: int = 30
    stale_after_min: int = 10
    # Score-normalization caps (not hard gates).
    change_pct_cap: float = 0.10
    rvol_cap: float = 5.0
    dollar_volume_cap: float = 500_000_000.0
    momentum_pct_cap: float = 0.05
    spread_bps_cap: float = 100.0
    # Component weights; renormalized when a metric is unavailable.
    weight_change: float = 0.35
    weight_rvol: float = 0.30
    weight_dollar_volume: float = 0.15
    weight_momentum: float = 0.15
    weight_spread: float = 0.05


@dataclass(frozen=True)
class HotStock:
    symbol: str
    last_price: float | None
    change_pct: float | None
    relative_volume: float | None
    cum_volume: float
    dollar_volume: float
    momentum_pct: float | None
    spread_bps: float | None
    score: float | None
    classification: str  # eligible | filtered | unavailable
    reasons: tuple[str, ...]
    bar_freshness: str  # fresh | stale | none
    quote_available: bool
    as_of: datetime | None


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _prior_close(sessions: dict, today) -> float | None:
    prior_days = sorted(d for d in sessions if d < today)
    if not prior_days:
        return None
    close = sessions[prior_days[-1]][-1].close
    return close if close and close > 0 else None


def _same_time_baseline(sessions: dict, today, cutoff, config: ScannerConfig):
    """Mean cumulative volume at the same time-of-day over prior sessions."""
    totals = []
    for d in sorted(sessions):
        if d >= today:
            continue
        cum = session_cum_volume_at(sessions[d], cutoff)
        if cum > 0:
            totals.append(cum)
    recent = totals[-config.baseline_sessions:]
    if len(recent) < config.min_baseline_sessions:
        return None
    return sum(recent) / len(recent)


def _momentum_pct(today_bars: list, window_min: int) -> float | None:
    """Return over the trailing `window_min` minutes of the session."""
    if len(today_bars) < 2:
        return None
    last_ts = today_bars[-1].timestamp
    cutoff = last_ts - timedelta(minutes=window_min)
    past = [b for b in today_bars if b.timestamp <= cutoff]
    if not past or not past[-1].close or past[-1].close <= 0:
        return None
    return today_bars[-1].close / past[-1].close - 1


def _score(
    change_pct: float | None,
    rvol: float | None,
    dollar_volume: float,
    momentum_pct: float | None,
    spread_bps: float | None,
    config: ScannerConfig,
) -> float:
    """Weighted 0-100 score; missing metrics drop out and weights renormalize."""
    parts = []
    if change_pct is not None:
        parts.append((config.weight_change, _clamp(change_pct / config.change_pct_cap)))
    if rvol is not None:
        parts.append((config.weight_rvol, _clamp(rvol / config.rvol_cap)))
    if dollar_volume > 0:
        span = math.log10(config.dollar_volume_cap / config.min_dollar_volume)
        dv_norm = math.log10(dollar_volume / config.min_dollar_volume) / span if span > 0 else 0.0
        parts.append((config.weight_dollar_volume, _clamp(dv_norm)))
    if momentum_pct is not None:
        parts.append((config.weight_momentum, _clamp(momentum_pct / config.momentum_pct_cap)))
    if spread_bps is not None:
        parts.append((config.weight_spread, 1 - _clamp(spread_bps / config.spread_bps_cap)))
    total_weight = sum(w for w, _ in parts)
    if total_weight <= 0:
        return 0.0
    return round(100 * sum(w * v for w, v in parts) / total_weight, 1)


def _evaluate(
    symbol: str,
    bars: list,
    quote,
    config: ScannerConfig,
    now: datetime,
) -> HotStock:
    today = now.astimezone(MARKET_TZ).date()
    sessions = bars_by_session(bars)
    today_bars = sessions.get(today, [])
    if not today_bars:
        return HotStock(
            symbol, None, None, None, 0, 0.0, None, None, None,
            "unavailable", ("no intraday bars",), "none", False, None,
        )

    as_of = today_bars[-1].timestamp
    last_price = today_bars[-1].close
    cum_volume = sum(b.volume for b in today_bars)
    dollar_volume = sum(b.close * b.volume for b in today_bars)

    if not last_price or last_price <= 0:
        return HotStock(
            symbol, last_price, None, None, cum_volume, dollar_volume, None,
            None, None, "unavailable", ("no usable price",),
            "stale" if now - as_of > timedelta(minutes=config.stale_after_min) else "fresh",
            False, as_of,
        )

    prior_close = _prior_close(sessions, today)
    change_pct = last_price / prior_close - 1 if prior_close else None

    cutoff = as_of.astimezone(MARKET_TZ).time()
    baseline = _same_time_baseline(sessions, today, cutoff, config)
    rvol = cum_volume / baseline if baseline else None

    momentum_pct = _momentum_pct(today_bars, config.momentum_window_min)
    spread_bps = spread_bps_from_quote(quote)

    staleness_min = (now - as_of).total_seconds() / 60
    bar_freshness = "stale" if staleness_min > config.stale_after_min else "fresh"
    quote_available = spread_bps is not None

    reasons = []
    if change_pct is None:
        reasons.append("prior close unavailable")
    elif change_pct * 100 < config.min_positive_change_pct:
        reasons.append(
            f"change {change_pct * 100:+.2f}% below +{config.min_positive_change_pct:.2f}% minimum"
        )
    if rvol is None:
        reasons.append("rvol unavailable (insufficient baseline)")
    elif config.min_rvol is not None and rvol < config.min_rvol:
        reasons.append(f"rvol {rvol:.2f}x below {config.min_rvol:.2f}x minimum")
    if dollar_volume < config.min_dollar_volume:
        reasons.append(
            f"dollar volume ${dollar_volume:,.0f} below ${config.min_dollar_volume:,.0f} minimum"
        )
    if last_price < config.min_price:
        reasons.append(f"price ${last_price:.2f} below ${config.min_price:.2f} minimum")
    if bar_freshness == "stale" and config.require_fresh_data:
        reasons.append(f"stale bars ({staleness_min:.0f}m old)")
    if not quote_available:
        reasons.append("no usable quote")

    score = _score(change_pct, rvol, dollar_volume, momentum_pct, spread_bps, config)
    classification = "eligible" if not reasons else "filtered"

    return HotStock(
        symbol, last_price, change_pct, rvol, cum_volume, dollar_volume,
        momentum_pct, spread_bps, score, classification, tuple(reasons),
        bar_freshness, quote_available, as_of,
    )


_CLASSIFICATION_ORDER = {"eligible": 0, "filtered": 1, "unavailable": 2}


def scan_hot_stocks(
    symbols: list[str],
    client=None,
    config: ScannerConfig | None = None,
    now: datetime | None = None,
) -> list[HotStock]:
    """Scan symbols with 1-minute bars.

    Ordering: eligible first, then filtered, then unavailable — each group by
    descending score. A high score never overrides classification.
    """
    config = config or ScannerConfig()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    client = client or get_data_client()

    bars_by_symbol = fetch_minute_bars(
        client, symbols, days=LOOKBACK_CALENDAR_DAYS, now=now
    )
    completed = {
        s: completed_minute_bars(bars, now) for s, bars in bars_by_symbol.items()
    }
    quotes = fetch_latest_quotes(client, symbols)

    results = [
        _evaluate(symbol, completed.get(symbol, []), quotes.get(symbol), config, now)
        for symbol in symbols
    ]
    return sorted(
        results,
        key=lambda r: (
            _CLASSIFICATION_ORDER.get(r.classification, 3),
            -(r.score or 0),
            r.symbol,
        ),
    )
