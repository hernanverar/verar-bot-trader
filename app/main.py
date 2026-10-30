import argparse

from app.scanner import StockMetrics, rank_stocks


def run_demo() -> None:
    demo_data = [
        StockMetrics("DEMO_A", 0.08, 1.8, 75_000_000, 6),
        StockMetrics("DEMO_B", 0.12, 2.3, 20_000_000, 18),
        StockMetrics("DEMO_C", -0.04, 0.9, 5_000_000, 40),
    ]
    print("DEMO ONLY — synthetic inputs, no real prices or trading")
    for ticker, score in rank_stocks(demo_data):
        print(f"{ticker}: {score}/100")


def run_scan(symbols: list[str]) -> None:
    from app.market_data import format_scan_results, scan_symbols

    print("Market-data scan — read-only, no orders are placed")
    results = scan_symbols(symbols)
    print(format_scan_results(results))


def main() -> None:
    parser = argparse.ArgumentParser(description="Stock opportunity scanner")
    parser.add_argument(
        "--scan",
        nargs="*",
        metavar="TICKER",
        help="fetch real daily bars and rank tickers (defaults to the built-in universe)",
    )
    parser.add_argument("--backtest", action="store_true", help="run a read-only historical simulation")
    args = parser.parse_args()

    if args.backtest:
        if args.scan is not None:
            parser.error("--scan and --backtest cannot be combined")
        from app.backtest import fetch_history, run_backtest, backtest_report
        from app.market_data import DEFAULT_SYMBOLS

        history = fetch_history(DEFAULT_SYMBOLS + ["SPY"])
        print(backtest_report(run_backtest(history)))
    elif args.scan is None:
        run_demo()
    else:
        from app.market_data import DEFAULT_SYMBOLS

        run_scan([s.upper() for s in args.scan] or DEFAULT_SYMBOLS)


if __name__ == "__main__":
    main()
