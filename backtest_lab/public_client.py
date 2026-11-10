"""Unauthenticated Delta India production candle downloader."""
import time
import requests
from datetime import datetime, timezone

BASE_URL = "https://api.india.delta.exchange"
MAX_CANDLES = 2000
SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}


class PublicDataError(RuntimeError):
    pass


class DeltaPublicHistoryClient:
    def __init__(self, base_url=BASE_URL, timeout=12):
        self.base_url, self.timeout = base_url.rstrip("/"), timeout

    def fetch(self, symbol, timeframe, start_ts, end_ts, progress=None):
        if timeframe not in SECONDS:
            raise ValueError("Unsupported timeframe")
        step = SECONDS[timeframe] * MAX_CANDLES
        all_rows = []
        cursor = int(start_ts)
        while cursor < end_ts:
            stop = min(cursor + step, end_ts)
            data = self._get({"symbol": symbol, "resolution": timeframe, "start": cursor, "end": stop})
            for item in data.get("result") or []:
                ts = int(item["time"])
                if start_ts <= ts < end_ts:
                    all_rows.append((ts, float(item["open"]), float(item["high"]), float(item["low"]),
                                     float(item["close"]), float(item.get("volume") or 0)))
            cursor = stop
            if progress:
                progress(cursor, end_ts)
            time.sleep(0.15)
        return sorted({row[0]: row for row in all_rows}.values())

    def _get(self, params):
        last = None
        for delay in (0.5, 1.0, 2.0):
            try:
                response = requests.get(self.base_url + "/v2/history/candles", params=params,
                    headers={"Accept": "application/json", "User-Agent": "smc-backtest-lab/1.0"}, timeout=self.timeout)
                if response.status_code == 429 or response.status_code >= 500:
                    last = PublicDataError(f"HTTP {response.status_code}")
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                data = response.json()
                if not data.get("success", False):
                    raise PublicDataError("Delta public API returned unsuccessful response")
                return data
            except (requests.RequestException, ValueError, PublicDataError) as exc:
                last = exc
                time.sleep(delay)
        raise PublicDataError(f"Candle download failed: {type(last).__name__}")
