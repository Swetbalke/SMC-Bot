"""Look-ahead-conscious bar backtest for the EMA + Engulfing paper strategy.

Signals use closed bars only; entries occur at the next bar open. If both SL and
TP are crossed in one bar, SL is assumed first. Fees and adverse slippage are
configurable. This is a simulator, not an exchange fill model.
"""
import argparse
import json
from pathlib import Path

import pandas as pd

from strategies.ema_engulfing_strategy import EMAEngulfingStrategy
from strategies.trade_levels import calculate_trade_levels
from strategies.stop_manager import propose_trailing_stop


TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "30m": 1800, "1h": 3600, "4h": 14400,
}


def prepare_historical_candles(candles: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Validate and normalize OHLCV data before a performance backtest."""
    required = {"time", "open", "high", "low", "close"}
    if candles is None or candles.empty or not required.issubset(candles.columns):
        raise ValueError("Historical candles must contain time/open/high/low/close")
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"Unsupported backtest timeframe: {timeframe}")

    df = candles.copy()
    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    if df["time"].isna().any():
        raise ValueError("Historical candles contain invalid timestamps")
    if df["time"].duplicated().any():
        raise ValueError("Historical candles contain duplicate timestamps")
    df = df.sort_values("time").reset_index(drop=True)
    for column in ("open", "high", "low", "close"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    valid = ((df[["open", "high", "low", "close"]] > 0).all(axis=1)
             & (df["high"] >= df[["open", "low", "close"]].max(axis=1))
             & (df["low"] <= df[["open", "high", "close"]].min(axis=1)))
    if not bool(valid.all()):
        raise ValueError("Historical CSV contains invalid OHLC rows")

    expected = TIMEFRAME_SECONDS[timeframe]
    gaps = df["time"].diff().dt.total_seconds().dropna()
    if bool((gaps > expected * 1.5).any()):
        raise ValueError("Historical candles contain timeframe gaps; repair or explicitly exclude them before backtesting")
    return df


def run_ema_backtest(candles: pd.DataFrame, config: dict) -> dict:
    timeframe = config.get("data", {}).get("timeframe", "1m")
    df = prepare_historical_candles(candles, timeframe)
    if len(df) < int(config.get("indicators", {}).get("ema_slow_period", 15)) + 4:
        raise ValueError("Not enough historical candles for EMA warm-up and trades")

    ind = config.get("indicators", {})
    risk = config.get("risk", {})
    general = config.get("general", {})
    exits = config.get("exit", {})
    execution = config.get("execution", {})
    previous_close = df["close"].shift(1)
    true_range = pd.concat([
        df["high"] - df["low"],
        (df["high"] - previous_close).abs(),
        (df["low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    atr_period = int(exits.get("atr_period", 14))
    atr_series = true_range.rolling(atr_period, min_periods=atr_period).mean()
    strategy_cfg = {k: ind.get(k) for k in (
        "ema_fast_period", "ema_slow_period", "ema_crossover_enabled",
        "bullish_engulfing_enabled", "bearish_engulfing_enabled",
        "engulfing_wait_candles", "ema_signal_mode")}
    strategy_cfg["trade_direction"] = config.get("entry", {}).get("trade_direction", "both")
    strategy_cfg["allow_long"] = config.get("entry", {}).get("allow_long", True)
    strategy_cfg["allow_short"] = config.get("entry", {}).get("allow_short", True)
    strategy = EMAEngulfingStrategy(strategy_cfg)
    capital = float(risk.get("capital", 1000.0))
    equity = capital
    peak = equity
    max_drawdown = 0.0
    fee_rate = float(execution.get("fee_rate_per_side", 0.0005))
    slip = float(execution.get("slippage_bps", 2.0)) / 10000.0
    pending_entry = None
    position = None
    trades = []
    rejected_signals = []
    total_costs = 0.0
    current_day = None
    day_trades = 0
    day_pnl = 0.0
    consecutive_losses = 0
    timezone_name = general.get("timezone", "Asia/Kolkata")

    def local_day(value):
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        return stamp.tz_convert(timezone_name).date()

    def entry_block_reason(signal_time):
        if not exits.get("mandatory_stop_loss", True):
            return "Mandatory stop loss disabled; entries locked"
        max_trades = int(config.get("entry", {}).get("max_daily_trades", 0))
        if max_trades and day_trades >= max_trades:
            return "Maximum daily trades reached"
        loss_limit = float(risk.get("max_daily_loss_amount", 0)) or capital * float(risk.get("max_daily_loss_pct", 0)) / 100.0
        if loss_limit and day_pnl <= -loss_limit:
            return "Maximum daily loss reached"
        target = capital * float(risk.get("max_daily_profit_pct", 0)) / 100.0
        if target and day_pnl >= target:
            return "Daily profit target reached"
        max_losses = int(risk.get("max_consecutive_losses", 0))
        if max_losses and consecutive_losses >= max_losses:
            return "Maximum consecutive losses reached"
        if risk.get("trading_session") == "custom":
            stamp = pd.Timestamp(signal_time)
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize("UTC")
            local_time = stamp.tz_convert(timezone_name).strftime("%H:%M")
            start_time = risk.get("trading_start_time", "00:00")
            end_time = risk.get("trading_end_time", "23:59")
            within = start_time <= local_time <= end_time if start_time <= end_time else (local_time >= start_time or local_time <= end_time)
            if not within:
                return "Outside configured trading session"
        return None

    def close_position(bar, raw_exit, reason):
        nonlocal position, equity, peak, max_drawdown, total_costs, day_pnl, consecutive_losses
        direction = position["direction"]
        exit_fill = raw_exit * (1.0 - slip if direction == "LONG" else 1.0 + slip)
        gross = ((exit_fill - position["entry"]) if direction == "LONG" else (position["entry"] - exit_fill)) * position["quantity"]
        fee = (position["entry"] * position["quantity"] + exit_fill * position["quantity"]) * fee_rate
        net = gross - fee
        total_costs += fee + abs(raw_exit - exit_fill) * position["quantity"]
        equity += net
        day_pnl += net
        consecutive_losses = consecutive_losses + 1 if net < 0 else 0
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak if peak > 0 else 0.0)
        trades.append({
            "signal_id": position["signal_id"], "direction": direction,
            "entry_time": position["entry_time"], "exit_time": str(bar["time"]),
            "entry_price": position["entry"], "exit_price": exit_fill,
            "stop_loss": position["sl"], "take_profit": position["tp"],
            "quantity": position["quantity"], "gross_pnl": gross,
            "fees": fee, "net_pnl": net, "exit_reason": reason,
            "planned_rr": position["planned_rr"],
            "realized_r": net / position["risk_amount"] if position["risk_amount"] > 0 else 0.0,
        })
        position = None

    for i in range(int(ind.get("ema_slow_period", 15)) + 2, len(df)):
        bar = df.iloc[i]
        bar_day = local_day(bar["time"])
        if bar_day != current_day:
            current_day = bar_day
            day_trades = 0
            day_pnl = 0.0
        # At this point the previous bar is fully closed. A signal detected there
        # can only enter at this bar's open.
        signal = strategy.process(df.iloc[:i], timeframe, forming_candle=False)
        if signal and position is None and pending_entry is None:
            blocked = entry_block_reason(bar["time"])
            if blocked:
                rejected_signals.append({"signal_id": signal.signal_id, "reason": blocked})
            else:
                pending_entry = {
                    "signal_id": signal.signal_id, "direction": signal.direction,
                    "stop_loss": signal.stop_loss,
                }
        if pending_entry is not None and position is None:
            direction = pending_entry["direction"]
            raw_entry = float(bar["open"])
            entry = raw_entry * (1.0 + slip if direction == "LONG" else 1.0 - slip)
            try:
                sl, calculated_tp = calculate_trade_levels(direction, entry, pending_entry["stop_loss"], exits,
                                                           ind.get("ema_sl_mode", "candle_extreme"))
                use_tp = bool(exits.get("use_take_profit", True))
                tp = calculated_tp if use_tp else 0.0
                risk_distance = abs(entry - sl)
                if (direction == "LONG" and sl >= entry) or (direction == "SHORT" and sl <= entry):
                    raise ValueError("Stop loss invalid after next-open gap")
                if risk.get("position_size_mode", "risk_percent") == "fixed_lot":
                    quantity = float(risk.get("fixed_lot_size", 0.01))
                else:
                    unit_risk = risk_distance + (entry + sl) * (fee_rate + slip)
                    minimum = float(risk.get("minimum_position_size", 0.001))
                    maximum = float(risk.get("maximum_position_size", 1.0))
                    quantity = max(minimum, min(equity * float(risk.get("risk_pct", 1.0)) / 100.0 / unit_risk, maximum))
                if risk_distance <= 0 or quantity <= 0:
                    raise ValueError("Risk/quantity invalid")
                if risk.get("position_size_mode", "risk_percent") != "fixed_lot":
                    budget = equity * float(risk.get("risk_pct", 1.0)) / 100.0
                    if quantity * (risk_distance + (entry + sl) * (fee_rate + slip)) > budget * 1.001:
                        raise ValueError("Minimum lot exceeds risk budget")
                day_trades += 1
                position = {
                    "signal_id": pending_entry["signal_id"], "direction": direction,
                    "entry_time": str(bar["time"]), "entry": entry,
                    "initial_sl_price": sl, "initial_risk_points": risk_distance,
                    "best_favorable_price": entry, "trailing_status": "active",
                    "rr_milestones_enabled": exits.get("trailing_method", "rr") != "fixed_points",
                    "sl_price": sl,
                    "sl": sl, "tp": tp, "quantity": quantity,
                    "planned_rr": abs(tp - entry) / risk_distance if use_tp else None,
                    "risk_amount": quantity * (risk_distance + (entry + sl) * (fee_rate + slip)),
                    "trailing_enabled": bool(exits.get("use_trailing_stop", False)),
                    "trailing_stop_pct": float(exits.get("trailing_stop_pct", 0.3)),
                    "trailing_method": exits.get("trailing_method", "rr"),
                    "trailing_stop_points": float(exits.get("trailing_stop_points", 50.0)),
                    "breakeven_trigger_rr": float(exits.get("breakeven_trigger_rr", 2.0)),
                    "profit_lock_trigger_rr": float(exits.get("profit_lock_trigger_rr", 3.0)),
                    "profit_lock_level_rr": float(exits.get("profit_lock_level_rr", 2.0)),
                    "fee_adjust_breakeven": bool(exits.get("fee_adjust_breakeven", False)),
                    "atr_multiplier": float(exits.get("atr_multiplier", 2.0)),
                    "entry_config": {"fee_rate_per_side": fee_rate,
                                     "slippage_bps": float(execution.get("slippage_bps", 2.0))},
                }
                total_costs += abs(raw_entry - entry) * quantity
            except (ValueError, TypeError, ZeroDivisionError):
                position = None
            pending_entry = None

        if position is not None:
            direction = position["direction"]
            o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
            if direction == "LONG":
                sl_hit = l <= position["sl"]
                tp_hit = exits.get("use_take_profit", True) and h >= position["tp"]
                if sl_hit:
                    close_position(bar, min(o, position["sl"]), "SL Hit")
                elif tp_hit:
                    close_position(bar, max(o, position["tp"]), "TP Hit")
            else:
                sl_hit = h >= position["sl"]
                tp_hit = exits.get("use_take_profit", True) and l <= position["tp"]
                if sl_hit:
                    close_position(bar, max(o, position["sl"]), "SL Hit")
                elif tp_hit:
                    close_position(bar, min(o, position["tp"]), "TP Hit")
            if position is not None and position.get("trailing_enabled"):
                # Bar-based simulation uses favorable high/low at bar close and
                # applies the confirmed stop on following bars (no same-bar lookahead).
                favorable = h if direction == "LONG" else l
                current_atr = float(atr_series.iloc[i]) if pd.notna(atr_series.iloc[i]) else None
                proposal = propose_trailing_stop(position, favorable, current_atr)
                if proposal and not proposal.get("triggered"):
                    position["sl"] = float(proposal["stop_price"])
                    position["sl_price"] = float(proposal["stop_price"])
                    position["best_favorable_price"] = float(proposal["best_price"])
                    position["trailing_status"] = proposal["status"]
        if position is not None:
            direction = position["direction"]
            mark = float(bar["close"])
            unrealized = ((mark - position["entry"]) if direction == "LONG" else (position["entry"] - mark)) * position["quantity"]
            estimated_close_cost = mark * position["quantity"] * (fee_rate + slip)
            marked_equity = equity + unrealized - estimated_close_cost
            peak = max(peak, marked_equity)
            max_drawdown = max(max_drawdown, (peak - marked_equity) / peak if peak > 0 else 0.0)

    if pending_entry is not None:
        # A final-bar signal has no subsequent open in the supplied history.
        pending_entry = None
    if position is not None:
        last = df.iloc[-1]
        close_position(last, float(last["close"]), "End of data")

    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] < 0]
    gross_profit = sum(t["net_pnl"] for t in wins)
    gross_loss = abs(sum(t["net_pnl"] for t in losses))
    direction_stats = {}
    for direction in ("LONG", "SHORT"):
        subset = [t for t in trades if t["direction"] == direction]
        direction_stats[direction] = {
            "trades": len(subset), "wins": sum(t["net_pnl"] > 0 for t in subset),
            "net_pnl": round(sum(t["net_pnl"] for t in subset), 4),
        }
    expectancy = sum(t["net_pnl"] for t in trades) / len(trades) if trades else 0.0
    # Daily realized-return Sharpe avoids annualising minute bars and avoids
    # treating every intraday trade as an independent calendar-day return.
    daily_returns = {}
    for trade in trades:
        day = pd.Timestamp(trade["exit_time"]).date().isoformat()
        daily_returns[day] = daily_returns.get(day, 0.0) + trade["net_pnl"] / capital
    returns = pd.Series(list(daily_returns.values()), dtype=float)
    sharpe = None
    if len(returns) >= 2 and float(returns.std(ddof=1)) > 0:
        sharpe = round(float(returns.mean() / returns.std(ddof=1) * (365 ** 0.5)), 4)
    return {
        "total_trades": len(trades), "winning_trades": len(wins), "losing_trades": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "net_profit_loss": round(sum(t["net_pnl"] for t in trades), 4),
        "maximum_drawdown_pct": round(max_drawdown * 100, 2),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else (None if gross_profit else 0.0),
        "average_risk_reward": round(sum(t["realized_r"] for t in trades) / len(trades), 3) if trades else 0.0,
        "expectancy_per_trade": round(expectancy, 4),
        "sharpe_ratio_daily": sharpe,
        "funding_costs": 0.0,
        "funding_model": "not_modelled",
        "average_planned_risk_reward": (
            round(sum(t["planned_rr"] for t in trades if t["planned_rr"] is not None)
                  / sum(t["planned_rr"] is not None for t in trades), 3)
            if any(t["planned_rr"] is not None for t in trades) else None
        ),
        "total_trading_costs": round(total_costs, 4),
        "rejected_signals": rejected_signals,
        "long_performance": direction_stats["LONG"], "short_performance": direction_stats["SHORT"],
        "trades": trades,
    }


def run_walk_forward_backtest(candles: pd.DataFrame, config: dict, in_sample_fraction: float = 0.70) -> dict:
    """Report fixed chronological in-sample and untouched out-of-sample results."""
    if not 0.5 <= in_sample_fraction < 1.0:
        raise ValueError("in_sample_fraction must be at least 0.5 and below 1.0")
    timeframe = config.get("data", {}).get("timeframe", "1m")
    df = prepare_historical_candles(candles, timeframe)
    split = int(len(df) * in_sample_fraction)
    slow = int(config.get("indicators", {}).get("ema_slow_period", 15))
    if split < slow + 4 or len(df) - split < slow + 4:
        raise ValueError("Not enough data in both walk-forward segments")
    return {
        "split": {"in_sample_rows": split, "out_of_sample_rows": len(df) - split,
                  "in_sample_end": df.iloc[split - 1]["time"].isoformat(),
                  "out_of_sample_start": df.iloc[split]["time"].isoformat()},
        "in_sample": run_ema_backtest(df.iloc[:split], config),
        "out_of_sample": run_ema_backtest(df.iloc[split:], config),
    }


def main():
    parser = argparse.ArgumentParser(description="Backtest the configured EMA strategy on a real OHLCV CSV")
    parser.add_argument("--csv", required=True, help="Historical CSV with time,open,high,low,close columns")
    parser.add_argument("--config", default="config/bot_settings.json", help="Settings JSON")
    args = parser.parse_args()
    if Path(args.csv).name.lower() == "btc_1m_sample.csv":
        raise SystemExit("Refusing performance backtest: btc_1m_sample.csv is synthetic demo data.")
    candles = pd.read_csv(args.csv)
    with open(args.config, "r", encoding="utf-8") as f:
        settings = json.load(f)
    result = run_walk_forward_backtest(candles, settings)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

