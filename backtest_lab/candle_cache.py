import pandas as pd
from .storage import cached_candles, cache_candles
from .public_client import DeltaPublicHistoryClient, SECONDS


def load_candles(symbol, timeframe, start_ts, end_ts, progress=None):
    rows = cached_candles(symbol, timeframe, start_ts, end_ts)
    expected = max(1, (end_ts - start_ts) // SECONDS[timeframe])
    if len(rows) < expected * 0.95:
        rows = DeltaPublicHistoryClient().fetch(symbol, timeframe, start_ts, end_ts, progress)
        cache_candles(symbol, timeframe, rows)
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    if df.empty:
        raise ValueError("No public candles available")
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    gaps = df["time"].diff().dt.total_seconds().gt(SECONDS[timeframe] * 1.5)
    return df, int(gaps.sum())
