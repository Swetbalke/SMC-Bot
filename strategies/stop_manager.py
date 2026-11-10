"""Protective stop validation and monotonic trailing-stop calculations."""
from math import isfinite


def validate_stop(direction, entry_price, stop_price, minimum_distance=0.01):
    """Return a clear error if a simulated protective stop is unsafe."""
    try:
        entry = float(entry_price)
        stop = float(stop_price)
        minimum = float(minimum_distance)
    except (TypeError, ValueError):
        return "Stop loss and entry price must be numeric"
    if not all(isfinite(value) for value in (entry, stop, minimum)):
        return "Stop loss values must be finite"
    if entry <= 0 or stop <= 0 or minimum <= 0:
        return "Entry, stop loss, and minimum distance must be greater than zero"
    if direction == "LONG" and stop >= entry:
        return "BUY stop loss must be below entry"
    if direction == "SHORT" and stop <= entry:
        return "SELL stop loss must be above entry"
    if direction not in {"LONG", "SHORT"}:
        return "Direction must be LONG or SHORT"
    if abs(entry - stop) < minimum:
        return f"Stop loss distance must be at least {minimum:g} points"
    return None


def calculate_initial_stop(direction, entry_price, mode="fixed_points", points=50.0,
                           signal_stop=None, custom_mode=None, custom_value=None):
    """Calculate a directional stop. EMA candle stops remain available."""
    entry = float(entry_price)
    if mode in {"engulfing_candle", "candle_extreme"}:
        if signal_stop is None:
            raise ValueError("Engulfing-candle stop requires the confirmed candle extreme")
        stop = float(signal_stop)
    elif mode == "fixed_points":
        distance = float(points)
        if distance <= 0 or not isfinite(distance):
            raise ValueError("Fixed stop points must be greater than zero")
        stop = entry - distance if direction == "LONG" else entry + distance
    elif mode == "custom":
        if custom_mode not in {"pct", "fixed"}:
            raise ValueError("Custom stop mode must be pct or fixed")
        value = float(custom_value)
        if value <= 0 or not isfinite(value):
            raise ValueError("Custom stop value must be greater than zero")
        distance = entry * value / 100.0 if custom_mode == "pct" else value
        stop = entry - distance if direction == "LONG" else entry + distance
    else:
        raise ValueError(f"Unsupported stop calculation mode: {mode}")
    if not isfinite(stop):
        raise ValueError("Calculated stop loss is not finite")
    return round(stop, 2)


def propose_trailing_stop(position, current_price, atr_value=None):
    """Return a candidate stop and state; never return a looser stop."""
    direction = position["direction"]
    entry = float(position["entry_price"])
    initial_sl = float(position.get("initial_sl_price", position["sl_price"]))
    current_sl = float(position["sl_price"])
    risk = float(position.get("initial_risk_points", abs(entry - initial_sl)))
    price = float(current_price)
    if (not all(isfinite(value) for value in (entry, initial_sl, current_sl, risk, price))
            or risk <= 0 or price <= 0 or direction not in {"LONG", "SHORT"}):
        return None

    previous_best = float(position.get("best_favorable_price") or entry)
    best = max(previous_best, price) if direction == "LONG" else min(previous_best, price)
    rr = ((best - entry) if direction == "LONG" else (entry - best)) / risk
    method = position.get("trailing_method", "rr")
    candidate = current_sl
    status = position.get("trailing_status", "active")

    be_trigger = float(position.get("breakeven_trigger_rr", 2.0))
    lock_trigger = float(position.get("profit_lock_trigger_rr", 3.0))
    lock_level = float(position.get("profit_lock_level_rr", 2.0))
    be_price = entry
    if position.get("fee_adjust_breakeven"):
        cfg = position.get("entry_config", {})
        fee_rate = float(cfg.get("fee_rate_per_side", 0.0))
        slip_rate = float(cfg.get("slippage_bps", 0.0)) / 10000.0
        adjustment = entry * (2.0 * fee_rate + 2.0 * slip_rate)
        be_price = entry + adjustment if direction == "LONG" else entry - adjustment
    # These safety milestones apply to newly opened positions. Database
    # migrations leave them off for legacy open trades to preserve behavior.
    # A fixed-points trail is a constant-gap rule: RR milestones must not
    # move its stop farther than the configured one-for-one trailing formula.
    milestones_enabled = bool(position.get("rr_milestones_enabled", 1)) and method != "fixed_points"
    if milestones_enabled and rr >= be_trigger:
        candidate = be_price
        status = "breakeven"
    if milestones_enabled and rr >= lock_trigger:
        locked_rr = lock_level + (rr - lock_trigger)
        candidate = entry + locked_rr * risk if direction == "LONG" else entry - locked_rr * risk
        status = "profit_locked"

    if method == "rr":
        # Continue to trail the favorable extreme by the configured lock gap.
        if rr >= lock_trigger:
            gap = (lock_trigger - lock_level) * risk
            rr_trail = best - gap if direction == "LONG" else best + gap
            candidate = max(candidate, rr_trail) if direction == "LONG" else min(candidate, rr_trail)
    elif method == "fixed_points":
        distance = float(position.get("trailing_stop_points", 50.0))
        if distance <= 0:
            distance = None
        if distance is not None:
            trail = best - distance if direction == "LONG" else best + distance
            candidate = max(candidate, trail) if direction == "LONG" else min(candidate, trail)
            if rr < lock_trigger and (trail > current_sl if direction == "LONG" else trail < current_sl):
                status = "trailing"
    elif method == "percentage":
        distance_pct = float(position.get("trailing_stop_pct", 0.3)) / 100.0
        if distance_pct <= 0:
            distance_pct = None
        if distance_pct is not None:
            trail = best * (1.0 - distance_pct if direction == "LONG" else 1.0 + distance_pct)
            candidate = max(candidate, trail) if direction == "LONG" else min(candidate, trail)
            if rr < lock_trigger and (trail > current_sl if direction == "LONG" else trail < current_sl):
                status = "trailing"
    elif method == "atr":
        if atr_value is not None and float(atr_value) > 0:
            multiplier = float(position.get("atr_multiplier", 2.0))
            distance = float(atr_value) * multiplier
            trail = best - distance if direction == "LONG" else best + distance
            candidate = max(candidate, trail) if direction == "LONG" else min(candidate, trail)
            if rr < lock_trigger and (trail > current_sl if direction == "LONG" else trail < current_sl):
                status = "trailing"
    else:
        return None

    candidate = round(candidate, 2)
    candidate = max(current_sl, candidate) if direction == "LONG" else min(current_sl, candidate)
    if direction == "LONG" and candidate >= price:
        # Caller treats this as a stop hit at the observed market price.
        return {"stop_price": candidate, "best_price": best, "rr": rr,
                "status": status, "triggered": True}
    if direction == "SHORT" and candidate <= price:
        return {"stop_price": candidate, "best_price": best, "rr": rr,
                "status": status, "triggered": True}
    return {"stop_price": candidate, "best_price": best, "rr": rr,
            "status": status, "triggered": False}
