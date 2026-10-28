from dataclasses import dataclass


@dataclass(frozen=True)
class StockMetrics:
    ticker: str
    momentum_20d: float  # decimal return, e.g. 0.05 = +5%
    relative_volume: float
    avg_daily_dollar_volume: float
    spread_bps: float


def score_stock(stock: StockMetrics) -> float | None:
    """Illustrative screening score, NOT a validated predictive model."""
    if stock.avg_daily_dollar_volume < 10_000_000 or stock.spread_bps > 30:
        return None
    momentum = min(max(stock.momentum_20d / 0.15, 0), 1)
    volume = min(max((stock.relative_volume - 1) / 2, 0), 1)
    spread_quality = 1 - min(stock.spread_bps / 30, 1)
    return round(100 * (0.5 * momentum + 0.3 * volume + 0.2 * spread_quality), 1)


def rank_stocks(stocks: list[StockMetrics]) -> list[tuple[str, float]]:
    results = [(s.ticker, score_stock(s)) for s in stocks]
    return sorted([(ticker, score) for ticker, score in results if score is not None], key=lambda row: row[1], reverse=True)
