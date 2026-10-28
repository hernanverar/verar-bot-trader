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

The demo uses synthetic sample data, not current market prices. Next steps: add an authenticated market-data adapter, point-in-time universe selection, historical backtesting with transaction costs, and a paper-only broker adapter.
