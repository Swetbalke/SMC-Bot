"""Deterministic bar backtester that imports only pure strategy helpers."""
import math
import pandas as pd
from strategy import detect_signals, calc_sl_tp, calc_quantity
from strategies.ema_engulfing_strategy import EMAEngulfingStrategy
from strategies.stop_manager import calculate_initial_stop


def _signal(candles, i, request, ema):
    cfg = request["strategy_config"]
    if request["strategy"] == "EMA Crossover + 100-Point Trail":
        result = ema.process(candles.iloc[:i + 1], request["timeframe"], forming_candle=True)
        return (result.direction, result.reason, result.stop_loss) if result else (None, "", None)
    direction, reason = detect_signals(candles.iloc[:i + 1], cfg["indicators"], cfg["entry"])
    return direction, reason, None


def run(candles, request):
    cfg = request["strategy_config"]
    costs = request["costs"]
    fee, slip = float(costs["fee_rate"]), float(costs["slippage_bps"]) / 10000
    ema = EMAEngulfingStrategy({**cfg["indicators"], **cfg["entry"]})
    trades, equity = [], [float(cfg["risk"]["capital"])]
    position = None
    warmup = max(25, int(cfg["indicators"].get("ema_slow_period", 15)) + 3)
    for i in range(warmup, len(candles)):
        bar = candles.iloc[i]
        if position:
            hit_sl = bar.low <= position["sl"] if position["direction"] == "LONG" else bar.high >= position["sl"]
            hit_tp = bar.high >= position["tp"] if position["direction"] == "LONG" else bar.low <= position["tp"]
            if hit_sl or hit_tp:  # Conservative: SL wins if both touch the same bar.
                raw_exit, why = (position["sl"], "SL Hit") if hit_sl else (position["tp"], "TP Hit")
                exit_price = raw_exit * (1 - slip if position["direction"] == "LONG" else 1 + slip)
                gross = ((exit_price - position["entry"]) if position["direction"] == "LONG" else (position["entry"] - exit_price)) * position["qty"]
                fees = (position["entry"] + exit_price) * position["qty"] * fee
                net = gross - fees
                risk = abs(position["entry"] - position["sl"]) * position["qty"]
                trades.append({**position, "exit_time": str(bar.time), "exit": exit_price, "reason": why, "net_pnl": net, "r": net / risk if risk else 0})
                equity.append(equity[-1] + net)
                position = None
            continue
        direction, reason, signal_stop = _signal(candles, i, request, ema)
        if not direction or i + 1 >= len(candles):
            continue
        next_open = float(candles.iloc[i + 1].open)
        entry = next_open * (1 + slip if direction == "LONG" else 1 - slip)
        if signal_stop is not None:
            sl = calculate_initial_stop(direction, entry, cfg["indicators"].get("ema_sl_mode", "fixed_points"),
                points=cfg["exit"].get("sl_points", 100), signal_stop=signal_stop)
        else:
            sl, _ = calc_sl_tp(direction, entry, cfg["exit"])
        risk_points = abs(entry - sl)
        if not risk_points:
            continue
        rr = float(cfg["risk"].get("risk_reward_ratio", 3))
        tp = entry + risk_points * rr if direction == "LONG" else entry - risk_points * rr
        qty = calc_quantity(entry, sl, cfg["risk"], fee, slip)
        position = {"direction": direction, "entry_time": str(candles.iloc[i + 1].time), "entry": entry,
                    "sl": sl, "tp": tp, "qty": qty, "reason": reason}
    return trades, equity
