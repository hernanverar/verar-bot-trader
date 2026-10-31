"""Automatic universe discovery via Alpaca's screener API. Read-only.

Uses the server-side market movers (top gainers) and most-actives screeners
to build a candidate universe, then hands it to the intraday scanner.

Data-source distinction: screener results are computed by Alpaca on the
consolidated tape (coverage depends on the account's data plan), while the
intraday evaluation in `app.strategies.hot_stock_scanner` uses the configured
bars feed (IEX by default). When the screener is unavailable we fall back to
a curated universe and say so explicitly — never silently.
"""

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from alpaca.common.exceptions import APIError
from alpaca.data.enums import MarketType, MostActivesBy
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import MarketMoversRequest, MostActivesRequest
from alpaca.trading.client import TradingClient
from dotenv import load_dotenv

from app.market_data import DEFAULT_SYMBOLS

load_dotenv()

DEFAULT_TOP = 25
DEFAULT_MAX_SYMBOLS = 30
MAX_SCREENER_ATTEMPTS = 2  # one retry max — bounded by design


@dataclass(frozen=True)
class DiscoveryResult:
    symbols: tuple[str, ...]
    source: str  # "screener:movers+actives" | "screener:movers" | "fallback_curated"
    as_of: datetime | None
    is_fallback: bool
    warning: str | None
    excluded_instruments_count: int = 0


def _api_keys() -> tuple[str, str]:
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY environment variables")
    return api_key, secret_key


def get_screener_client() -> ScreenerClient:
    api_key, secret_key = _api_keys()
    return ScreenerClient(api_key, secret_key)


def get_trading_client() -> TradingClient:
    """Paper trading client — used ONLY for asset metadata, never orders."""
    api_key, secret_key = _api_keys()
    return TradingClient(api_key=api_key, secret_key=secret_key, paper=True)


def _is_transient(exc: APIError) -> bool:
    status = exc.status_code
    return status is None or status == 429 or status >= 500


def _fallback_universe(now: datetime, reason: str) -> DiscoveryResult:
    return DiscoveryResult(
        symbols=tuple(DEFAULT_SYMBOLS),
        source="fallback_curated",
        as_of=now,
        is_fallback=True,
        warning=(
            f"Screener discovery unavailable ({reason}); using curated "
            f"fallback universe of {len(DEFAULT_SYMBOLS)} symbols — "
            "NOT market-wide discovery."
        ),
    )


# Suffixes after the final "." mapped to non-common-stock instrument types.
# Other dotted suffixes (e.g. BRK.B, HEI.A) are legitimate class shares.
_SUFFIX_TYPES = {
    "WS": "warrant",
    "RT": "right",
    "U": "unit",
    "UN": "unit",
    "WI": "unsupported",  # when-issued
}
_PREFERRED_SUFFIX = re.compile(r"^(P[A-Z]|PR[A-Z]?)$")
_NAME_RULES = [
    ("warrant", "warrant"),
    ("right", "right"),
    ("unit", "unit"),
    ("preferred", "preferred"),
    ("preference", "preferred"),
]
# Exchange-traded products are not common stocks: ETFs/ETNs plus
# leveraged/inverse ETPs identified by fund naming conventions.
_ETP_NAME_PATTERNS = [
    r"\bet[fnp]\b",              # ETF, ETN, ETP
    r"\bexchange[- ]traded\b",
    r"\b(inverse|leveraged|ultra|ultrapro)\b",
    r"\b\d+(\.\d+)?x\b",         # 2X, 3X, 1.5X multipliers
    r"\b(bull|bear)\b",
]
_NAME_RULES = [
    ("warrant", "warrant"),
    ("right", "right"),
    ("unit", "unit"),
    ("preferred", "preferred"),
    ("preference", "preferred"),
]

_ASSET_CACHE: dict[str, object | None] = {}
_ASSET_CACHE_MAX = 512


def _suffix_instrument(symbol: str) -> str | None:
    suffix = symbol.rsplit(".", 1)[-1].upper() if "." in symbol else ""
    if suffix in _SUFFIX_TYPES:
        return _SUFFIX_TYPES[suffix]
    if _PREFERRED_SUFFIX.match(suffix):
        return "preferred"
    return None


def classify_instrument(symbol: str, asset=None) -> str:
    """common_stock | adr | warrant | right | unit | preferred |
    unsupported | unverified — from suffix safeguards plus Alpaca metadata."""
    suffix_kind = _suffix_instrument(symbol)
    if suffix_kind is not None:
        return suffix_kind

    if asset is None:
        return "unverified"

    asset_class = getattr(asset, "asset_class", getattr(asset, "class_", None))
    if asset_class is not None and "equity" not in str(asset_class).lower():
        return "unsupported"
    if getattr(asset, "tradable", True) is False:
        return "unsupported"

    name = (getattr(asset, "name", "") or "").lower()
    if "american depositary" in name or re.search(r"\badr\b", name):
        return "adr"
    for keyword, instrument in _NAME_RULES:
        if re.search(rf"\b{keyword}s?\b", name):
            return instrument
    for pattern in _ETP_NAME_PATTERNS:
        if re.search(pattern, name):
            return "etf"
    return "common_stock"


def _get_asset(trading_client, symbol: str):
    if symbol in _ASSET_CACHE:
        return _ASSET_CACHE[symbol]
    try:
        asset = trading_client.get_asset(symbol)
    except Exception:
        asset = None
    if len(_ASSET_CACHE) >= _ASSET_CACHE_MAX:
        _ASSET_CACHE.clear()
    _ASSET_CACHE[symbol] = asset
    return asset


def _filter_instruments(trading_client, symbols: list[str]) -> tuple[list[str], int]:
    """Keep verified common stocks/ADRs; return (kept, excluded_count).

    With a trading client: definitive metadata decides — unverifiable
    symbols are excluded rather than silently assumed common. Without one:
    suffix safeguards only (best-effort, documented).
    """
    kept, excluded = [], 0
    for symbol in symbols:
        if trading_client is None:
            if _suffix_instrument(symbol) is None:
                kept.append(symbol)
            else:
                excluded += 1
            continue
        kind = classify_instrument(symbol, asset=_get_asset(trading_client, symbol))
        if kind in ("common_stock", "adr"):
            kept.append(symbol)
        else:
            excluded += 1
    return kept, excluded


def discover_momentum_universe(
    top: int = DEFAULT_TOP,
    include_actives: bool = True,
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
    screener_client=None,
    trading_client=None,
    now: datetime | None = None,
) -> DiscoveryResult:
    """Discover candidate symbols from Alpaca's screener.

    Gainers first, then most-actives; deduplicated in arrival order and
    capped at `max_symbols`. Falls back to the curated universe — flagged —
    whenever the screener is unreachable or the account lacks permission.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    screener_client = screener_client or get_screener_client()

    try:
        movers = None
        for attempt in range(MAX_SCREENER_ATTEMPTS):
            try:
                movers = screener_client.get_market_movers(
                    MarketMoversRequest(top=top, market_type=MarketType.STOCKS)
                )
                break
            except APIError as exc:
                if attempt == MAX_SCREENER_ATTEMPTS - 1 or not _is_transient(exc):
                    raise
                time.sleep(0.5)
    except APIError as exc:
        return _fallback_universe(now, f"market movers error: {exc}")
    except Exception as exc:
        return _fallback_universe(now, f"{type(exc).__name__}: {exc}")

    symbols = [m.symbol for m in getattr(movers, "gainers", [])]
    last_updated = getattr(movers, "last_updated", None)
    source = "screener:movers"

    if include_actives:
        try:
            actives = screener_client.get_most_actives(
                MostActivesRequest(top=top, by=MostActivesBy.VOLUME)
            )
            symbols.extend(a.symbol for a in getattr(actives, "most_actives", []))
            source = "screener:movers+actives"
            if last_updated is None:
                last_updated = getattr(actives, "last_updated", None)
        except APIError:
            pass  # gainers alone are still a valid discovery source

    symbols = [
        s for s in dict.fromkeys(symbols)
        if isinstance(s, str) and s.replace(".", "").isalnum() and len(s) <= 12
    ]

    symbols, excluded = _filter_instruments(trading_client, symbols)

    symbols = symbols[:max_symbols]
    if not symbols:
        return _fallback_universe(now, "screener returned no usable symbols")

    return DiscoveryResult(
        symbols=tuple(symbols),
        source=source,
        as_of=last_updated or now,
        is_fallback=False,
        warning=None,
        excluded_instruments_count=excluded,
    )
