import os

from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

load_dotenv()

def main():
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        print("ERROR: Missing Alpaca API credentials in .env")
        return

    client = TradingClient(
        api_key=api_key,
        secret_key=secret_key,
        paper=True,
    )

    account = client.get_account()

    print("Alpaca Paper Trading connected!")
    print(f"Account status: {account.status}")
    print(f"Paper equity: ${account.equity}")
    print(f"Paper cash: ${account.cash}")


if __name__ == "__main__":
    main()
