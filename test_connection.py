"""Read-only Delta India Demo/Testnet connection check.

Run with Demo credentials in DELTA_API_KEY and DELTA_API_SECRET:
    python test_connection.py

This script never submits, edits, or cancels an order.
"""
import os
import sys

from delta_exchange import DeltaAPIError, DeltaDemoClient


def main() -> int:
    environment = os.environ.get("DELTA_ENV", "testnet").strip().lower()
    if environment != "testnet":
        print("FAIL: DELTA_ENV must be 'testnet'. This project never connects to production.")
        return 2

    try:
        client = DeltaDemoClient()
        client.get_server_time()
        product = client.get_product("BTCUSD")
        product_id = product.get("id")
        wallets = client.get_wallet_balances()
        position = client.get_position(product_id)
    except DeltaAPIError as exc:
        print(f"FAIL: {exc}")
        return 1
    except Exception as exc:
        print(f"FAIL: unexpected {type(exc).__name__}")
        return 1

    print("OK: Delta India Demo/Testnet authenticated")
    print("OK: server time endpoint returned a valid response")
    print(f"OK: BTCUSD product id = {product_id}")
    print(f"OK: product status = {product.get('trading_status', 'unknown')}")
    settlement = product.get("settling_asset") or {}
    asset_symbol = settlement.get("symbol")
    settlement_wallet = next((wallet for wallet in wallets
                              if str(wallet.get("asset_symbol", "")).upper() == str(asset_symbol).upper()), None)
    if settlement_wallet is None:
        print(f"WARN: wallets returned = {len(wallets)}; no {asset_symbol or 'settlement'} wallet was matched")
    else:
        print(f"OK: {asset_symbol} settlement balance = {settlement_wallet.get('balance', 'unknown')}")
    print(f"OK: BTCUSD position size = {int((position or {}).get('size') or 0)}")
    print("OK: server clock offset was synchronized during client initialization")
    print("No order was placed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
