"""Entry/SL/TP calculations and directional safety checks."""


def calculate_trade_levels(direction, entry_price, signal_stop, exit_config, ema_sl_mode="candle_extreme"):
    entry = float(entry_price)
    anchor = float(signal_stop)
    sl_mode = ema_sl_mode
    if sl_mode in {"candle_extreme", "engulfing_candle"}:
        stop = anchor
    elif sl_mode == "fixed_points":
        distance = float(exit_config.get("sl_points", 50.0))
        stop = entry - distance if direction == "LONG" else entry + distance
    elif sl_mode == "pct":
        distance = entry * float(exit_config.get("sl_value", 0.5)) / 100.0
        stop = entry - distance if direction == "LONG" else entry + distance
    elif sl_mode == "fixed":
        distance = float(exit_config.get("sl_value", 0.5))
        stop = entry - distance if direction == "LONG" else entry + distance
    else:
        raise ValueError(f"Unsupported EMA stop-loss mode: {sl_mode}")

    risk = abs(entry - stop)
    if not (entry > 0 and risk > 0 and stop > 0):
        raise ValueError("Trade risk is zero or invalid")
    if (direction == "LONG" and stop >= entry) or (direction == "SHORT" and stop <= entry):
        raise ValueError("Stop loss is on the wrong side of entry")

    tp_mode = exit_config.get("tp_mode", "risk_reward")
    if tp_mode == "risk_reward":
        distance = risk * float(exit_config.get("risk_reward_ratio", 2.0))
    elif tp_mode == "pct":
        distance = entry * float(exit_config.get("tp_value", 1.5)) / 100.0
    elif tp_mode == "fixed":
        distance = float(exit_config.get("tp_value", 1.5))
    else:
        raise ValueError(f"Unsupported take-profit mode: {tp_mode}")
    if distance <= 0:
        raise ValueError("Take-profit distance is zero or invalid")
    target = entry + distance if direction == "LONG" else entry - distance
    target, stop = round(target, 2), round(stop, 2)
    if (direction == "LONG" and target <= entry) or (direction == "SHORT" and target >= entry):
        raise ValueError("Take profit is on the wrong side of entry")
    return stop, target

