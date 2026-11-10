"""
test_clock_drift.py — Delta testnet clock drift check + signature fix test
Run: python test_clock_drift.py
"""
import requests
import time
import sys

BASE_URL = "https://cdn-ind.testnet.deltaex.org"


def check_drift():
    print("=" * 55)
    print("  Delta Testnet — Clock Drift Diagnostic")
    print("=" * 55)

    # 1. Ping /v2/time
    print("\n[1] Fetching Delta server time...")
    try:
        t_before = time.time()
        resp = requests.get(BASE_URL + "/v2/time",
                            headers={"Accept": "application/json"},
                            timeout=8)
        t_after = time.time()
    except Exception as e:
        print(f"    ERROR reaching {BASE_URL}/v2/time: {e}")
        return False

    print(f"    HTTP {resp.status_code}")
    try:
        data = resp.json()
    except Exception:
        print(f"    Non-JSON response: {resp.text[:200]}")
        return False

    print(f"    Raw response: {str(data)[:300]}")

    # 2. Parse server timestamp
    result = data.get("result") or {}
    server_ms = (
        result.get("server_time")
        or result.get("timestamp")
        or data.get("server_time")
        or data.get("timestamp")
    )

    if server_ms is None:
        print("\n    WARNING: /v2/time endpoint exists but no timestamp field found.")
        print("    Drift correction will fall back to local clock (offset=0).")
        # Still OK — _sync_time_offset() handles this silently
        return True

    # Convert millis → seconds if needed
    server_sec = (
        int(server_ms) / 1000.0
        if int(server_ms) > 1_000_000_000_000
        else float(server_ms)
    )
    local_mid = (t_before + t_after) / 2.0
    drift = server_sec - local_mid

    print(f"\n[2] Clock comparison")
    print(f"    Local  time  : {int(local_mid)}  ({time.strftime('%H:%M:%S', time.localtime(local_mid))})")
    print(f"    Server time  : {int(server_sec)}  ({time.strftime('%H:%M:%S', time.localtime(server_sec))})")
    print(f"    Drift        : {drift:+.3f} seconds")

    if abs(drift) > 5:
        print(f"\n    ⚠️  Drift {drift:+.1f}s EXCEEDS Delta's ±5s window!")
        print("    Root cause of expired_signature 401.")
        print("    Fixed: delta_exchange.py now compensates automatically.")
    else:
        print(f"\n    ✅  Drift is within ±5s — signature should work fine.")

    # 3. Simulate what the fixed _server_timestamp() returns
    corrected = int(time.time() + drift)
    print(f"\n[3] Corrected timestamp that will be sent: {corrected}")
    return True


def test_import():
    print("\n[4] Importing updated DeltaDemoClient...")
    try:
        from delta_exchange import DeltaDemoClient, DeltaAPIError
        # Instantiate without real keys — expect credential error, NOT import error
        try:
            c = DeltaDemoClient(api_key="x", api_secret="y")
            print("    Client created (dummy creds) — sync_time_offset ran OK")
        except DeltaAPIError as e:
            if "credentials" in str(e).lower():
                print("    Import OK; credential guard works as expected.")
            else:
                print(f"    Unexpected DeltaAPIError: {e}")
        except Exception as e:
            print(f"    ERROR: {e}")
        print("    ✅  delta_exchange.py import successful")
    except Exception as e:
        print(f"    ❌  Import failed: {e}")


if __name__ == "__main__":
    ok = check_drift()
    test_import()
    print("\n" + "=" * 55)
    print("  Restart engine: Close existing windows, then run")
    print("  Start_Trading.bat")
    print("=" * 55)
