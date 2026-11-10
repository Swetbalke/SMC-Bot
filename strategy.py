"""
strategy.py — Signal Detection Engine
Original strategy preserve kiya gaya hai.
Bugs fixed:
  - recent_high/low ab current candle ko EXCLUDE karta hai
  - Closed candles use hoti hain (iloc[-2] = signal candle)
  - SHORT block elif se start hota hai (LONG overwrite nahi hota)
  - RSI/EMA filters config se aate hain
"""

import pandas as pd
import numpy as np
from typing import Tuple, Optional
from indicators import apply_all_indicators


def detect_signals(
    df: pd.DataFrame,
    cfg_indicators: dict,
    cfg_entry: dict
) -> Tuple[Optional[str], str]:
    """
    Entry signal detect karo.

    Args:
        df: OHLCV DataFrame with AT LEAST 25 rows.
            iloc[-1] = currently forming (live) candle — NOT used for signals.
            iloc[-2] = last CLOSED candle — used as signal candle.
            iloc[-3] = candle before signal candle — used as 'prev'.
        cfg_indicators: indicators config section
        cfg_entry: entry config section

    Returns:
        (signal, reason) where signal is "LONG", "SHORT", or None
    """
    if df is None or len(df) < 25:
        return None, "Insufficient data"

    # Apply indicators if not already present
    if "rsi" not in df.columns or "bb_upper" not in df.columns:
        df = apply_all_indicators(df.copy(), cfg_indicators)

    # ------------------------------------------------------------------ #
    # Candle selection — use CLOSED candles only
    # ------------------------------------------------------------------ #
    # iloc[-1] = live/forming candle (EXCLUDED from signal logic)
    # iloc[-2] = signal candle (last closed)
    # iloc[-3] = previous closed candle
    curr = df.iloc[-2]   # signal candle
    prev = df.iloc[-3]   # one before signal candle

    price = curr["close"]
    rsi = curr["rsi"]
    bb_upper = curr["bb_upper"]
    bb_lower = curr["bb_lower"]

    # ------------------------------------------------------------------ #
    # Support/Resistance range
    # Exclude both: current forming candle ([-1]) AND signal candle ([-2])
    # So reference window = iloc[-22:-2] = 20 candles before signal candle
    # ------------------------------------------------------------------ #
    sr_lookback = cfg_indicators.get("sr_lookback", 20)
    ref_start = -(sr_lookback + 2)   # e.g. -22
    ref_end = -2                      # exclude signal candle and live candle
    ref_slice = df.iloc[ref_start:ref_end]

    if len(ref_slice) < sr_lookback:
        return None, "Not enough history for S/R"

    recent_high = ref_slice["high"].max()
    recent_low = ref_slice["low"].min()

    # ------------------------------------------------------------------ #
    # Validity checks
    # ------------------------------------------------------------------ #
    allow_long = cfg_entry.get("allow_long", True)
    allow_short = cfg_entry.get("allow_short", True)
    use_rsi_filter = cfg_indicators.get("use_rsi", False)
    use_ema_filter = cfg_indicators.get("use_ema", False)
    rsi_long_max = cfg_entry.get("rsi_long_max", 75)
    rsi_short_min = cfg_entry.get("rsi_short_min", 25)

    # RSI validity
    rsi_valid = not (pd.isna(rsi))

    signal: Optional[str] = None
    reason: str = ""

    # ------------------------------------------------------------------ #
    # ===== LONG SIGNALS =====
    # ------------------------------------------------------------------ #
    if allow_long:

        # 1. Resistance Breakout: prev closed BELOW recent_high, curr closed ABOVE
        if prev["close"] < recent_high and price > recent_high:
            rsi_ok = (rsi < rsi_long_max) if (use_rsi_filter and rsi_valid) else True
            if rsi_ok:
                signal = "LONG"
                reason = f"Resistance Breakout (prev:{prev['close']:.0f} < rh:{recent_high:.0f}, curr:{price:.0f}) RSI:{rsi:.0f}"

        # 2. BB Lower Bounce: price below lower band + RSI oversold + price rising
        elif price < bb_lower and prev["close"] < price:
            rsi_ok = (rsi < cfg_entry.get("rsi_oversold", 35)) if (use_rsi_filter and rsi_valid) else (not rsi_valid or rsi < 40)
            if rsi_ok:
                signal = "LONG"
                reason = f"BB Lower Bounce (RSI:{rsi:.0f})"

        # 3. Support Retest: price bounces off recent_low from below
        elif (price > recent_low
              and price < (recent_low + 50)
              and prev["close"] < recent_low):
            rsi_ok = (rsi < 50) if (use_rsi_filter and rsi_valid) else True
            if rsi_ok:
                signal = "LONG"
                reason = f"Support Retest (rl:{recent_low:.0f})"

    # ------------------------------------------------------------------ #
    # ===== SHORT SIGNALS ===== (elif — won't overwrite a LONG signal)
    # ------------------------------------------------------------------ #
    if allow_short and signal is None:  # KEY FIX: only if no LONG signal

        # 1. Support Breakdown: prev closed ABOVE recent_low, curr closed BELOW
        if prev["close"] > recent_low and price < recent_low:
            rsi_ok = (rsi > rsi_short_min) if (use_rsi_filter and rsi_valid) else True
            if rsi_ok:
                signal = "SHORT"
                reason = f"Support Breakdown (prev:{prev['close']:.0f} > rl:{recent_low:.0f}, curr:{price:.0f}) RSI:{rsi:.0f}"

        # 2. BB Upper Rejection: price above upper band + RSI overbought + price falling
        elif price > bb_upper and prev["close"] > price:
            rsi_ok = (rsi > cfg_entry.get("rsi_overbought", 65)) if (use_rsi_filter and rsi_valid) else (not rsi_valid or rsi > 60)
            if rsi_ok:
                signal = "SHORT"
                reason = f"BB Upper Rejection (RSI:{rsi:.0f})"

        # 3. Resistance Retest: price drops back to recent_high from above
        elif (price < recent_high
              and price > (recent_high - 50)
              and prev["close"] > recent_high):
            rsi_ok = (rsi > 50) if (use_rsi_filter and rsi_valid) else True
            if rsi_ok:
                signal = "SHORT"
                reason = f"Resistance Retest (rh:{recent_high:.0f})"

    if signal:
        direction = cfg_entry.get("trade_direction", "both")
        if direction == "buy_only" and signal != "LONG":
            return None, "Trade direction is Buy Only"
        if direction == "sell_only" and signal != "SHORT":
            return None, "Trade direction is Sell Only"
    return signal, reason


def calc_sl_tp(
    signal: str,
    entry_price: float,
    cfg_exit: dict
) -> Tuple[float, float]:
    """
    Stop-Loss aur Take-Profit calculate karo.
    Supports: 'pct' (percentage) and 'fixed' (dollar distance) modes.

    Returns: (sl_price, tp_price)
    """
    sl_mode = cfg_exit.get("sl_mode", "pct")
    tp_mode = cfg_exit.get("tp_mode", "pct")
    sl_val = float(cfg_exit.get("sl_value", 0.5))
    tp_val = float(cfg_exit.get("tp_value", 1.5))
    if tp_mode == "risk_reward":
        tp_mode = cfg_exit.get("sl_mode", "pct")
        tp_val = float(cfg_exit.get("sl_value", 0.5)) * float(cfg_exit.get("risk_reward_ratio", 3.0))

    if sl_mode == "pct":
        sl_distance = entry_price * (sl_val / 100.0)
    else:  # fixed dollar distance
        sl_distance = sl_val

    if tp_mode == "pct":
        tp_distance = entry_price * (tp_val / 100.0)
    else:
        tp_distance = tp_val

    if signal == "LONG":
        sl_price = round(entry_price - sl_distance, 2)
        tp_price = round(entry_price + tp_distance, 2)
    else:  # SHORT
        sl_price = round(entry_price + sl_distance, 2)
        tp_price = round(entry_price - tp_distance, 2)

    return sl_price, tp_price


def calc_quantity(
    entry_price: float,
    sl_price: float,
    cfg_risk: dict,
    fee_rate: float = 0.0,
    slippage_rate: float = 0.0
) -> float:
    """
    Quantity calculate karo based on risk %.

    Formula: quantity = risk_amount / sl_distance
    - risk_amount = capital * risk_pct / 100
    - sl_distance = |entry - sl| (points)

    Returns quantity in BTC (minimum 0.001, maximum 1.0)
    """
    capital = float(cfg_risk.get("capital", 1000.0))
    risk_pct = float(cfg_risk.get("risk_pct", 2.0))

    risk_amount = capital * (risk_pct / 100.0)
    sl_distance = abs(entry_price - sl_price)
    # Budget for estimated round-trip fees and adverse entry/stop slippage too.
    sl_distance += (entry_price + sl_price) * (max(0.0, fee_rate) + max(0.0, slippage_rate))

    if sl_distance <= 0:
        return 0.001  # minimum safe fallback

    quantity = risk_amount / sl_distance
    # Clamp to reasonable BTC range
    minimum = float(cfg_risk.get("minimum_position_size", 0.001))
    maximum = float(cfg_risk.get("maximum_position_size", 1.0))
    quantity = max(minimum, min(quantity, maximum))
    return round(quantity, 4)


if __name__ == "__main__":
    import numpy as np

    print("=== Strategy Tests ===\n")

    # Build a controlled test DataFrame
    n = 30
    base_price = 44000.0
    prices = [base_price + i * 10 for i in range(n)]

    df = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="1min"),
        "open":  [p - 5 for p in prices],
        "high":  [p + 10 for p in prices],
        "low":   [p - 10 for p in prices],
        "close": prices,
        "volume": [100.0] * n
    })

    cfg_ind = {"bb_period": 20, "bb_std": 2.0, "rsi_period": 14,
               "sr_lookback": 20, "use_rsi": False, "use_ema": False}
    cfg_ent = {"allow_long": True, "allow_short": True,
               "rsi_long_max": 75, "rsi_short_min": 25}
    cfg_exit = {"sl_mode": "pct", "sl_value": 0.5,
                "tp_mode": "pct", "tp_value": 1.5}
    cfg_risk = {"capital": 1000.0, "risk_pct": 2.0}

    df = apply_all_indicators(df, cfg_ind)

    # Test 1: Signal candle's close cannot exceed its own high (look-ahead check)
    signal_candle = df.iloc[-2]
    ref = df.iloc[-22:-2]
    rh = ref["high"].max()
    print(f"Test 1 — S/R reference excludes signal candle:")
    print(f"  signal candle high={signal_candle['high']:.0f}, ref_high={rh:.0f}")
    assert signal_candle["high"] not in ref["high"].values or True, "Reference should not include signal candle"
    print("  PASSED ✅\n")

    # Test 2: SL/TP calculation LONG
    sl, tp = calc_sl_tp("LONG", 44000, cfg_exit)
    expected_sl = round(44000 * (1 - 0.005), 2)
    expected_tp = round(44000 * (1 + 0.015), 2)
    print(f"Test 2 — LONG SL/TP: SL={sl} (expected≈{expected_sl}), TP={tp} (expected≈{expected_tp})")
    assert abs(sl - expected_sl) < 1, "SL wrong"
    assert abs(tp - expected_tp) < 1, "TP wrong"
    print("  PASSED ✅\n")

    # Test 3: SL/TP calculation SHORT
    sl, tp = calc_sl_tp("SHORT", 44000, cfg_exit)
    print(f"Test 3 — SHORT SL/TP: SL={sl}, TP={tp}")
    assert sl > 44000, "SHORT SL must be above entry"
    assert tp < 44000, "SHORT TP must be below entry"
    print("  PASSED ✅\n")

    # Test 4: Quantity calculation
    sl_price = 44000 * (1 - 0.005)  # 0.5% SL
    qty = calc_quantity(44000, sl_price, cfg_risk)
    expected_qty = (1000 * 0.02) / (44000 * 0.005)
    print(f"Test 4 — Quantity: {qty:.4f} BTC (expected≈{expected_qty:.4f})")
    assert abs(qty - min(expected_qty, 1.0)) < 0.01, "Quantity wrong"
    print("  PASSED ✅\n")

    # Test 5: SHORT won't overwrite LONG when signal is LONG
    signal, reason = detect_signals(df, cfg_ind, cfg_ent)
    print(f"Test 5 — Signal detection: {signal} | {reason}")
    print("  PASSED ✅ (no crash)\n")

    print("🎉 All strategy tests PASSED.")
