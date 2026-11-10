"""EMA helpers used by the EMA crossover strategy."""
import pandas as pd


def calculate_emas(close: pd.Series, fast_period: int = 9, slow_period: int = 15):
    if fast_period < 1 or slow_period <= fast_period:
        raise ValueError("Slow EMA period must be greater than Fast EMA period")
    fast = close.astype(float).ewm(span=fast_period, adjust=False, min_periods=fast_period).mean()
    slow = close.astype(float).ewm(span=slow_period, adjust=False, min_periods=slow_period).mean()
    return fast, slow

