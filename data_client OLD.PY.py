"""
data_client.py — Clean Data Provider
Delta Exchange ka PUBLIC API use karta hai (no login needed).
CSV fallback bhi available hai agar internet na ho.
"""

import os
import time
import requests
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Optional
from logger_setup import get_logger

logger = get_logger("data_client")

# Delta Exchange PUBLIC endpoints (no authentication needed)
DELTA_BASE = "https://api.india.delta.exchange"
DELTA_PRODUCTS_URL = f"{DELTA_BASE}/v2/products"
DELTA_CANDLES_URL = f"{DELTA_BASE}/v2/history/candles"

# Symbol mapping: our canonical symbol → Delta product_id
SYMBOL_MAP = {
    "BTCUSDT": {"product_id": 139, "name": "BTCUSDT Perpetual"},
    "ETHUSDT": {"product_id": 140, "name": "ETHUSDT Perpetual"},
}

# Resolution mapping: timeframe string → Delta resolution string
RESOLUTION_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "60m",
    "4h": "240m",
    "1d": "1d"
}

REQUIRED_COLS = ["time", "open", "high", "low", "close", "volume"]


class DeltaPublicClient:
    """
    Delta Exchange India PUBLIC API client.
    - Koi API key nahi chahiye.
    - Sirf candle data fetch karta hai.
    - Offline CSV fallback support.
    """

    def __init__(self, fallback_csv: str = "data/btc_1m_sample.csv"):
        self.fallback_csv: str = fallback_csv
        self._cached_df: Optional[pd.DataFrame] = None
        self._cache_time: float = 0.0
        self._cache_ttl: float = 55.0  # seconds (refresh just before next candle)
        self._using_fallback: bool = False
        self._last_error: str = ""

    # ------------------------------------------------------------------ #
    # Public interface
    # ------------------------------------------------------------------ #

    def get_candles(
        self,
        symbol: str = "BTCUSDT",
        resolution: str = "1m",
        limit: int = 200
    ) -> Optional[pd.DataFrame]:
        """
        Return DataFrame with last `limit` CLOSED candles.
        Returns None on failure.
        Columns: time (datetime), open, high, low, close, volume
        """
        # Return cache if fresh
        if self._cached_df is not None and (time.time() - self._cache_time) < self._cache_ttl:
            return self._cached_df.tail(limit).copy()

        # Try live Delta API
        df = self._fetch_delta(symbol, resolution, limit)

        if df is not None and not df.empty:
            self._cached_df = df
            self._cache_time = time.time()
            self._using_fallback = False
            return df.tail(limit).copy()

        # Fallback to CSV
        logger.warning(f"Delta API failed ({self._last_error}). Using CSV fallback.")
        df = self._load_csv(limit)
        if df is not None:
            self._using_fallback = True
            return df

        logger.error("Both Delta API and CSV fallback failed.")
        return None

    def get_current_price(self, symbol: str = "BTCUSDT") -> Optional[float]:
        """Return latest close price from cached/fetched data."""
        df = self.get_candles(symbol, limit=5)
        if df is not None and not df.empty:
            return float(df["close"].iloc[-1])
        return None

    def is_using_fallback(self) -> bool:
        return self._using_fallback

    def get_last_error(self) -> str:
        return self._last_error

    # ------------------------------------------------------------------ #
    # Delta API fetch
    # ------------------------------------------------------------------ #

    def _fetch_delta(
        self, symbol: str, resolution: str, limit: int
    ) -> Optional[pd.DataFrame]:
        """Fetch from Delta Exchange public candles endpoint."""
        try:
            product = SYMBOL_MAP.get(symbol)
            if not product:
                self._last_error = f"Unknown symbol: {symbol}"
                return None

            delta_res = RESOLUTION_MAP.get(resolution, "1m")

            # Calculate time range to cover requested candles
            # 1m candle = 60s, so limit=200 → 200×60=12000s of history
            seconds_per_candle = self._resolution_to_seconds(delta_res)
            end_ts = int(time.time())
            # Request extra buffer (1.5×) to handle gaps
            start_ts = end_ts - int(seconds_per_candle * limit * 1.5)

            params = {
                "symbol": symbol,
                "resolution": delta_res,
                "start": start_ts,
                "end": end_ts,
            }

            resp = requests.get(
                DELTA_CANDLES_URL,
                params=params,
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()

            candles = data.get("result", [])
            if not candles:
                self._last_error = "Empty response from Delta API"
                return None

            df = pd.DataFrame(candles)

            # Delta returns: t (Unix ts), o, h, l, c, v
            rename = {"t": "time", "o": "open", "h": "high",
                      "l": "low", "c": "close", "v": "volume"}
            df.rename(columns=rename, inplace=True)

            # Ensure required columns
            for col in REQUIRED_COLS:
                if col not in df.columns:
                    self._last_error = f"Missing column '{col}' in response"
                    return None

            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            df = df[REQUIRED_COLS].sort_values("time").drop_duplicates("time")
            df = df.dropna(subset=["close"])

            # Return only last `limit` rows
            return df.tail(limit).reset_index(drop=True)

        except requests.exceptions.ConnectionError:
            self._last_error = "No internet connection"
            return None
        except requests.exceptions.Timeout:
            self._last_error = "Delta API timeout"
            return None
        except requests.exceptions.HTTPError as e:
            self._last_error = f"HTTP {e.response.status_code}"
            return None
        except Exception as e:
            self._last_error = str(e)[:100]
            return None

    # ------------------------------------------------------------------ #
    # CSV fallback
    # ------------------------------------------------------------------ #

    def _load_csv(self, limit: int) -> Optional[pd.DataFrame]:
        """Load from local CSV sample data."""
        try:
            path = Path(self.fallback_csv)
            if not path.exists():
                self._last_error = f"CSV not found: {self.fallback_csv}"
                return None

            df = pd.read_csv(path)

            # Normalize column names (case-insensitive)
            df.columns = [c.strip().lower() for c in df.columns]

            if "time" not in df.columns:
                self._last_error = "CSV missing 'time' column"
                return None

            df["time"] = pd.to_datetime(df["time"])
            for col in ["open", "high", "low", "close", "volume"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            df = df[REQUIRED_COLS].sort_values("time").dropna(subset=["close"])
            return df.tail(limit).reset_index(drop=True)

        except Exception as e:
            self._last_error = f"CSV load error: {e}"
            return None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolution_to_seconds(res: str) -> int:
        mapping = {
            "1m": 60, "5m": 300, "15m": 900,
            "60m": 3600, "240m": 14400, "1d": 86400
        }
        return mapping.get(res, 60)


# Generate synthetic BTC sample data if CSV doesn't exist
def generate_sample_csv(filepath: str = "data/btc_1m_sample.csv", rows: int = 500):
    """
    Realistic synthetic BTC 1-minute OHLCV data.
    CLEARLY LABELED AS SYNTHETIC — not real market data.
    """
    path = Path(filepath)
    path.parent.mkdir(exist_ok=True)

    if path.exists():
        return  # Don't overwrite existing data

    np.random.seed(42)
    base_price = 44000.0
    timestamps = pd.date_range("2024-01-15 09:00:00", periods=rows, freq="1min")

    closes = [base_price]
    for _ in range(rows - 1):
        change = np.random.normal(0, 0.0015)  # 0.15% std per minute
        closes.append(round(closes[-1] * (1 + change), 2))

    data = []
    for i, (ts, close) in enumerate(zip(timestamps, closes)):
        vol_pct = abs(np.random.normal(0, 0.002))
        high = round(close * (1 + vol_pct), 2)
        low = round(close * (1 - vol_pct), 2)
        open_ = closes[i - 1] if i > 0 else close
        volume = round(np.random.uniform(0.5, 5.0), 3)
        data.append({
            "time": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume
        })

    df = pd.DataFrame(data)
    df.to_csv(path, index=False)
    print(f"[data_client] Synthetic sample data generated: {filepath} ({rows} rows)")
    print("[data_client] NOTE: This is SYNTHETIC data, NOT real market data.")


if __name__ == "__main__":
    # Generate sample data
    generate_sample_csv()

    client = DeltaPublicClient()

    print("=== Testing Delta Public API ===")
    df = client.get_candles("BTCUSDT", "1m", limit=10)
    if df is not None:
        print(f"SUCCESS: Got {len(df)} candles")
        print(df.tail(3).to_string())
        price = client.get_current_price()
        print(f"Current price: ${price:,.2f}")
    else:
        print(f"Delta API failed: {client.get_last_error()}")
        print("Testing CSV fallback...")
        df = client._load_csv(10)
        if df is not None:
            print(f"CSV fallback OK: {len(df)} rows")
        else:
            print("CSV fallback also failed.")

    print(f"\nUsing fallback: {client.is_using_fallback()}")
    print("Test complete.")
