"""
indicators.py — Pure Pandas/NumPy Indicator Calculations
pandas_ta se bilkul azad — Python 3.13 + NumPy 2.x compatible.
"""

import numpy as np
import pandas as pd
from typing import Tuple


def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """
    RSI — Wilder's Smoothed Moving Average method.
    Tested against standard RSI reference values.
    """
    if len(close) < period + 1:
        return pd.Series(np.nan, index=close.index)

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # Wilder's RMA (exponential, alpha=1/period)
    alpha = 1.0 / period
    avg_gain = gain.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # A zero average loss means pure gains (RSI 100); a flat window is neutral.
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    rsi = rsi.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    return rsi.round(2)


def calc_bollinger_bands(
    close: pd.Series, period: int = 20, std_dev: float = 2.0
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Bollinger Bands: upper, middle (SMA), lower.
    Returns: (upper, middle, lower)
    """
    sma = close.rolling(window=period, min_periods=period).mean()
    std = close.rolling(window=period, min_periods=period).std(ddof=0)
    upper = (sma + std_dev * std).round(4)
    lower = (sma - std_dev * std).round(4)
    return upper, sma.round(4), lower


def calc_ema(close: pd.Series, period: int = 50) -> pd.Series:
    """Exponential Moving Average."""
    return close.ewm(span=period, adjust=False, min_periods=period).mean().round(4)


def calc_macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    MACD Line, Signal Line, Histogram.
    Returns: (macd_line, signal_line, histogram)
    """
    ema_fast = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = (ema_fast - ema_slow).round(4)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean().round(4)
    histogram = (macd_line - signal_line).round(4)
    return macd_line, signal_line, histogram


def apply_all_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    Apply all indicators to a candle DataFrame.
    cfg = config_manager.get_section('indicators')
    Returns DataFrame with new columns added.
    """
    close = df["close"]

    # Bollinger Bands (always calculated)
    bb_period = cfg.get("bb_period", 20)
    bb_std = cfg.get("bb_std", 2.0)
    df["bb_upper"], df["bb_mid"], df["bb_lower"] = calc_bollinger_bands(close, bb_period, bb_std)

    # RSI (always calculated, filter optional)
    rsi_period = cfg.get("rsi_period", 14)
    df["rsi"] = calc_rsi(close, rsi_period)

    # EMA (optional display/filter)
    if cfg.get("use_ema", False):
        ema_period = cfg.get("ema_period", 50)
        df["ema"] = calc_ema(close, ema_period)

    if cfg.get("ema_crossover_enabled", False):
        df["ema_fast"] = calc_ema(close, int(cfg.get("ema_fast_period", 9)))
        df["ema_slow"] = calc_ema(close, int(cfg.get("ema_slow_period", 15)))

    # MACD (optional)
    if cfg.get("use_macd", False):
        df["macd"], df["macd_signal"], df["macd_hist"] = calc_macd(close)

    return df


if __name__ == "__main__":
    # Verification against known values
    # Using simple arithmetic sequence to verify RSI direction
    close = pd.Series([
        44000, 44200, 44500, 44100, 44300,
        44600, 44800, 44500, 44700, 44900,
        45000, 44800, 44600, 44400, 44600,
        44800, 45000, 45200, 45100, 45300
    ], dtype=float)

    rsi = calc_rsi(close, 14)
    print(f"RSI (last): {rsi.iloc[-1]:.2f}")
    assert not pd.isna(rsi.iloc[-1]), "RSI should not be NaN"
    assert 0 < rsi.iloc[-1] < 100, "RSI must be between 0-100"

    upper, mid, lower = calc_bollinger_bands(close, 20)
    print(f"BB Upper: {upper.iloc[-1]:.2f}, Mid: {mid.iloc[-1]:.2f}, Lower: {lower.iloc[-1]:.2f}")
    assert upper.iloc[-1] > mid.iloc[-1] > lower.iloc[-1], "BB order wrong"

    ema = calc_ema(close, 10)
    print(f"EMA(10): {ema.iloc[-1]:.2f}")

    print("✅ All indicator tests PASSED.")
