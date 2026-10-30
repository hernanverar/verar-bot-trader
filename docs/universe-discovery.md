# Universe Discovery Assessment — Phase 2A.1

Can we automatically discover top-gaining and high-volume U.S. stocks
instead of scanning the hardcoded 6-symbol universe?

## Endpoints available in alpaca-py 0.44.0

| Endpoint | SDK surface | Purpose | Subscription |
|---|---|---|---|
| Market movers (gainers/losers) | `ScreenerClient.get_market_movers(MarketMoversRequest(top, market_type))` | Server-side ranked top N gainers/losers | Screener API; availability depends on the account's data plan — Alpaca has historically gated screener endpoints differently from bars. Verify against this account (a 403 means it requires a plan upgrade). |
| Most actives | `ScreenerClient.get_most_actives(MostActivesRequest(top, by))` | Top N by consolidated volume or trade count | Same as above. |
| Tradable assets | `TradingClient.get_all_assets(AssetRequest)` | Full tradable U.S. equity universe (~10k+ symbols) | No market-data subscription needed — it is a trading API endpoint and works with paper keys. |
| Snapshots | `StockHistoricalDataClient.get_stock_snapshot` | Latest trade/quote/daily bar per symbol | Same plan rules as bars/quotes. |
| Daily/minute bars | `StockBarsRequest` (already used) | Batch pre-filtering by volume/gap | Same plan rules. |

## Does the IEX feed provide sufficient coverage?

- **For the current curated universe: yes, with caveats.** RVOL ratios and
  % change remain meaningful because both numerator and baseline come from
  the same feed. However, IEX reports only ~1–3% of consolidated U.S.
  volume, so absolute gates like `min_dollar_volume` understate true
  liquidity and would need lower thresholds.
- **For full-market discovery: no.** IEX covers only IEX-listed/executed
  prints, so a scanner built on IEX bars systematically misses or
  under-ranks symbols whose activity happens on other venues. Thin names
  may also produce `no usable quote` / `stale` results frequently.
- The server-side screener endpoints (movers/most-actives) are computed by
  Alpaca on the consolidated tape where the account's plan permits — they
  do not inherit our feed's coverage limits, which makes them the better
  discovery primitive if the plan allows.

## Recommended expansion path

1. **Try screener endpoints first** (one call each): fetch top ~25 movers
   and most-actives, union them, and feed that list into the existing
   `scan_hot_stocks`. Cheap, server-ranked, no local heavy lifting.
   Wrap in try/except — fall back on 403/permission errors.
2. **Fallback without screener access**: `get_all_assets` → filter to
   common stocks above a price floor → batch **daily** bars to pre-filter
   by gap % and average dollar volume → run the minute-bar scan on the
   surviving shortlist (~50–200 symbols). This costs one batched daily-bars
   request plus one batched minute-bars request and stays inside Alpaca's
   ~200 req/min data limit because all calls are multi-symbol.
3. **Optional hybrid**: maintain a curated list of liquid, high-beta names
   (current universe + sector leaders) as the default when discovery is
   unavailable.

## Practical notes

- Screener/movers output should be deduplicated and capped before the
  minute-bar scan (minute bars × many symbols is the expensive call).
- Extended-hours movers may show `stale`/`unavailable` intraday statuses —
  the classification model already reports these honestly.
- Do not hardcode screener availability: the endpoint must degrade
  gracefully to the curated universe.
