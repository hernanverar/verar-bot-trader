from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.backtest import fetch_history, run_backtest
from app.market_data import DEFAULT_SYMBOLS

app = FastAPI(
    title="VERAR BOT TRADER API",
    description="Trading research and paper trading API",
    version="0.1.0",
)


class BacktestRequest(BaseModel):
    initial_cash: float = Field(default=1000.0, gt=0, le=1_000_000)
    hold_sessions: int = Field(default=5, ge=1, le=100)
    top_n: int = Field(default=3, ge=1, le=20)


@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "service": "verar-bot-trader",
        "version": "0.1.0",
        "trading_mode": "paper",
    }


@app.post("/api/backtest")
def execute_backtest(request: BacktestRequest):
    """Run a read-only historical simulation. Never place broker orders."""
    try:
        symbols = list(dict.fromkeys([*DEFAULT_SYMBOLS, "SPY"]))
        history = fetch_history(symbols)
        result = run_backtest(
            history,
            initial_cash=request.initial_cash,
            hold_sessions=request.hold_sessions,
            top_n=request.top_n,
        )
        return asdict(result)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc