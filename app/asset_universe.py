"""US-listed equity universe via raw Alpaca REST (avoids the TXSE SDK enum bug).

CURRENT-ASSET HISTORICAL SCREEN — the manifest lists assets tradable TODAY;
it cannot represent delisted/historical symbols (survivorship bias, declared).
No secrets are logged or stored in manifests.
"""

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent / "data" / "research"
_LOCK = threading.Lock()
LISTED_EXCHANGES = {"NASDAQ", "NYSE", "AMEX"}
_EXCLUDED_CLASSES = {"warrant", "right", "unit", "preferred", "etf", "crypto"}


def _cred_headers() -> dict:
    key = os.getenv("ALPACA_API_KEY")
    sec = os.getenv("ALPACA_SECRET_KEY")
    if not key or not sec:
        raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}


@dataclass(frozen=True)
class UniverseManifest:
    acquired_ts: str
    source: str
    total_assets: int
    eligible: int
    excluded: dict          # reason -> count
    rules: dict


def fetch_asset_universe(
    base_url: str = "https://paper-api.alpaca.markets",
    timeout: int = 30,
    max_retries: int = 3,
) -> tuple[list[str], UniverseManifest]:
    """Fetch + filter the current US-listed equity universe.

    Returns (sorted symbols, manifest). Uses raw REST so unknown enum
    values (e.g. TXSE) can't break parsing.
    """
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            r = requests.get(
                f"{base_url}/v2/assets",
                params={"asset_class": "us_equity", "status": "active"},
                headers=_cred_headers(), timeout=timeout)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            assets = r.json()
            break
        except requests.RequestException as exc:
            last_err = exc
            time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f"asset universe fetch failed: {last_err}")

    excluded: dict[str, int] = {}
    symbols: list[str] = []
    for a in assets:
        sym = a.get("symbol", "")
        cls = (a.get("class") or "").lower()
        exch = a.get("exchange", "")
        reason = None
        if not a.get("tradable"):
            reason = "not_tradable"
        elif exch not in LISTED_EXCHANGES:
            reason = f"exchange_{(exch or 'none').lower()}"
        elif cls in _EXCLUDED_CLASSES:
            reason = f"class_{cls}"
        elif not sym.isalnum():
            reason = "nonstandard_symbol"
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
        else:
            symbols.append(sym)
    manifest = UniverseManifest(
        acquired_ts=datetime.now().astimezone().isoformat(),
        source="alpaca /v2/assets (raw REST)",
        total_assets=len(assets),
        eligible=len(symbols),
        excluded=excluded,
        rules={
            "exchanges": sorted(LISTED_EXCHANGES),
            "excluded_classes": sorted(_EXCLUDED_CLASSES),
            "status": "active", "tradable": True,
            "note": "CURRENT-ASSET screen — delisted symbols absent "
                    "(survivorship bias, cannot be reconstructed free)",
        })
    return sorted(set(symbols)), manifest


def save_manifest(manifest: UniverseManifest, symbols: list[str]) -> Path:
    d = ROOT / "universe"
    d.mkdir(parents=True, exist_ok=True)
    body = {"manifest": manifest.__dict__, "symbols": symbols}
    path = d / f"universe_{manifest.acquired_ts[:10]}.json"
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with _LOCK, os.fdopen(fd, "w") as f:
        json.dump(body, f)
    os.replace(tmp, path)
    return path


def chunked(seq: list, size: int = 120) -> list[list]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]
