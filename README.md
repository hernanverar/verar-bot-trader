# Trading Bot Starter

Python starter for a US-stock opportunity scanner. **No live orders are implemented.**

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
python -m app.main
```

The demo uses synthetic sample data, not current market prices.

## Market-data scan

`python -m app.main --scan` fetches real daily bars and latest quotes via the
Alpaca market-data API and ranks the default universe (NVDA, TSLA, GME, AMC,
AAPL, MSFT). Pass tickers to override: `python -m app.main --scan AMD INTC`.

The scan is **read-only** — it places no orders. It uses only completed daily
candles (today's bar is excluded until after the 16:00 ET close) and prints each
symbol's score alongside its latest market-data timestamp. Symbols with
insufficient bars or no usable quote are reported with a reason instead of a
score.

Credentials come from `.env` (see `.env.example`): `ALPACA_API_KEY`,
`ALPACA_SECRET_KEY`, and optionally `ALPACA_DATA_FEED` (`iex` for free plans,
`sip` for paid).

## Intraday hot-stock scanner

`GET /api/scanner/hot-stocks` ranks the default universe (or
`?symbols=A,B,C`) on 1-minute bars: intraday % change vs prior close,
relative volume vs a same-time-of-day baseline over prior sessions, dollar
volume, 30-minute momentum, and quoted spread. Optional `?top=` and
`?min_rvol=` parameters. Scores are a transparent ranking heuristic, not a
return forecast. Read-only — no orders.

Feed caveat: the default `iex` feed only reports IEX executions, so volumes
understate the consolidated tape. `sip` requires a paid subscription and is
delayed ~15 minutes on free plans, so near-real-time scanning needs IEX.

Next steps: point-in-time universe selection, historical backtesting with transaction costs, and a paper-only broker adapter.
