from app.scanner import StockMetrics, score_stock, rank_stocks


def test_rejects_illiquid_stock():
    assert score_stock(StockMetrics("X", 0.2, 3, 1000, 5)) is None


def test_score_is_bounded():
    assert 0 <= score_stock(StockMetrics("Y", 10, 100, 50_000_000, 0)) <= 100


def test_rank_descending():
    stocks = [StockMetrics("A", 0.01, 1, 20_000_000, 10), StockMetrics("B", 0.1, 2, 20_000_000, 10)]
    assert rank_stocks(stocks)[0][0] == "B"
