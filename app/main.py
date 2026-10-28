from app.scanner import StockMetrics, rank_stocks


def main() -> None:
    demo_data = [
        StockMetrics("DEMO_A", 0.08, 1.8, 75_000_000, 6),
        StockMetrics("DEMO_B", 0.12, 2.3, 20_000_000, 18),
        StockMetrics("DEMO_C", -0.04, 0.9, 5_000_000, 40),
    ]
    print("DEMO ONLY — synthetic inputs, no real prices or trading")
    for ticker, score in rank_stocks(demo_data):
        print(f"{ticker}: {score}/100")


if __name__ == "__main__":
    main()
