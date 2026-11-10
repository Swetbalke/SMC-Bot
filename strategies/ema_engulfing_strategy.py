"""Stateful, closed-candle EMA crossover followed by engulfing strategy."""
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import numpy as np

from .candlestick_patterns import bearish_engulfing, bullish_engulfing
from .ema_calculator import calculate_emas
from .signal_manager import SignalManager


@dataclass(frozen=True)
class StrategySignal:
    signal_id: str
    direction: str  # LONG / SHORT; UI maps these to BUY / SELL.
    signal_time: str
    crossover_time: Optional[str]
    entry_reference: float
    stop_loss: float
    reason: str


class EMAEngulfingStrategy:
    def __init__(self, config: dict):
        self.config = dict(config)
        self.manager = SignalManager()
        self.last_processed_time: Optional[str] = None
        self._seen_ids = set()

    def reset(self, config: Optional[dict] = None):
        if config is not None:
            self.config = dict(config)
        self.manager.clear()
        self.last_processed_time = None
        self._seen_ids.clear()

    @staticmethod
    def _time(value) -> str:
        return pd.Timestamp(value).isoformat()

    def _signal(self, direction, row, crossover_time, stop_loss, kind) -> Optional[StrategySignal]:
        signal_time = self._time(row["time"])
        crossover_key = crossover_time or signal_time
        sid = f"ema15:{direction}:{crossover_key}:{signal_time}"
        if sid in self._seen_ids:
            return None
        self._seen_ids.add(sid)
        self.manager.last_emitted_signal_id = sid
        return StrategySignal(
            sid, direction, signal_time, crossover_time,
            float(row["close"]), float(stop_loss), kind
        )

    def process(self, candles: pd.DataFrame, timeframe="1m", forming_candle=True):
        """Consume unseen closed candles and return at most one newly confirmed signal.

        When forming_candle is true, the final dataframe row is excluded. The caller
        should set it from the market-data client's candle status, never guess.
        """
        if candles is None or candles.empty:
            self.manager.clear()
            return None
        required = {"time", "open", "high", "low", "close"}
        if not required.issubset(candles.columns):
            self.manager.clear()
            return None
        closed = candles.iloc[:-1].copy() if forming_candle else candles.copy()
        if len(closed) < int(self.config.get("ema_slow_period", 15)) + 2:
            return None
        for column in ("open", "high", "low", "close"):
            closed[column] = pd.to_numeric(closed[column], errors="coerce")
        closed = closed.dropna(subset=["time", "open", "high", "low", "close"])
        if closed.empty:
            self.manager.clear()
            return None
        closed["time"] = pd.to_datetime(closed["time"], errors="coerce", utc=True)
        finite = np.isfinite(closed[["open", "high", "low", "close"]]).all(axis=1)
        valid_ohlc = ((closed["open"] > 0) & (closed["high"] > 0) & (closed["low"] > 0)
                      & (closed["close"] > 0) & (closed["high"] >= closed[["open", "low", "close"]].max(axis=1))
                      & (closed["low"] <= closed[["open", "high", "close"]].min(axis=1)))
        closed = closed[finite & valid_ohlc]
        closed = closed.dropna(subset=["time"]).sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)
        if closed.empty:
            self.manager.clear()
            return None

        fast_period = int(self.config.get("ema_fast_period", 9))
        slow_period = int(self.config.get("ema_slow_period", 15))
        fast, slow = calculate_emas(closed["close"], fast_period, slow_period)
        direction_mode = self.config.get("trade_direction", "both")
        allow_long = bool(self.config.get("allow_long", True))
        allow_short = bool(self.config.get("allow_short", True))
        crossover_enabled = bool(self.config.get("ema_crossover_enabled", True))
        max_wait = max(1, int(self.config.get("engulfing_wait_candles", 3)))
        mode = self.config.get("ema_signal_mode", "sequence")
        emitted = None

        # On process restart reconstruct only recent state. Old historical signals
        # are never emitted; a signal may only be returned for the newest closed bar.
        start = 0
        if self.last_processed_time is None:
            start = max(1, len(closed) - max_wait - 2)
        else:
            matching = closed.index[closed["time"].map(self._time) == self.last_processed_time]
            if len(matching):
                start = int(matching[-1]) + 1
            else:
                # A candle gap/data reset invalidates stale setups.
                self.manager.clear()
                start = max(1, len(closed) - 2)

        tf_minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}.get(timeframe)
        for i in range(max(1, start), len(closed)):
            row, prev = closed.iloc[i], closed.iloc[i - 1]
            t = self._time(row["time"])
            gap = (row["time"] - prev["time"]).total_seconds()
            if tf_minutes and gap > tf_minutes * 60 * 1.5:
                self.manager.clear()
                # Do not create a cross using non-adjacent candles.
                continue

            if pd.isna(fast.iloc[i]) or pd.isna(fast.iloc[i - 1]) or pd.isna(slow.iloc[i]) or pd.isna(slow.iloc[i - 1]):
                continue
            bull_cross = fast.iloc[i - 1] <= slow.iloc[i - 1] and fast.iloc[i] > slow.iloc[i]
            bear_cross = fast.iloc[i - 1] >= slow.iloc[i - 1] and fast.iloc[i] < slow.iloc[i]
            cross_dir = "LONG" if bull_cross else "SHORT" if bear_cross else None
            if cross_dir == "LONG" and direction_mode == "sell_only":
                cross_dir = None
            if cross_dir == "SHORT" and direction_mode == "buy_only":
                cross_dir = None
            if cross_dir == "LONG" and not allow_long:
                cross_dir = None
            if cross_dir == "SHORT" and not allow_short:
                cross_dir = None

            if direction_mode == "sell_only":
                bull_engulf = False
                bear_engulf = bearish_engulfing(prev, row)
            elif direction_mode == "buy_only":
                bull_engulf = bullish_engulfing(prev, row)
                bear_engulf = False
            else:
                bull_engulf = bullish_engulfing(prev, row)
                bear_engulf = bearish_engulfing(prev, row)
            # Direction-specific switches are applied separately below.
            bull_enabled = bool(self.config.get("bullish_engulfing_enabled", True)) and direction_mode != "sell_only" and allow_long
            bear_enabled = bool(self.config.get("bearish_engulfing_enabled", True)) and direction_mode != "buy_only" and allow_short

            pending = self.manager.pending
            if pending and cross_dir == ("SHORT" if pending.direction == "LONG" else "LONG"):
                self.manager.clear()
                pending = None

            # A crossover cannot be confirmed by an engulfing pattern on that same candle.
            if pending and i > pending.crossover_index:
                pending.candles_waited += 1
                if pending.candles_waited <= max_wait:
                    matched = (pending.direction == "LONG" and bull_enabled and bull_engulf) or (pending.direction == "SHORT" and bear_enabled and bear_engulf)
                    if matched:
                        if i == len(closed) - 1:
                            stop = float(row["low"] if pending.direction == "LONG" else row["high"])
                            emitted = self._signal(pending.direction, row, pending.crossover_time, stop, "EMA crossover followed by engulfing")
                        self.manager.clear()
                        if emitted:
                            break
                else:
                    self.manager.clear()

            if crossover_enabled and cross_dir:
                if mode == "crossover_only":
                    if i == len(closed) - 1:
                        stop = float(row["low"] if cross_dir == "LONG" else row["high"])
                        emitted = self._signal(cross_dir, row, t, stop, "EMA crossover")
                    self.manager.clear()
                    if emitted:
                        break
                elif mode in {"sequence", "any", "all", "either"}:
                    # Sequence mode must never silently degrade to a plain EMA
                    # cross when that side's engulfing confirmation is disabled.
                    confirmation_enabled = bull_enabled if cross_dir == "LONG" else bear_enabled
                    if confirmation_enabled:
                        self.manager.start(cross_dir, t, i)
                    else:
                        self.manager.clear()

            if not crossover_enabled or mode == "engulfing_only":
                if i == len(closed) - 1 and bull_enabled and bull_engulf:
                    emitted = self._signal("LONG", row, None, float(row["low"]), "Bullish engulfing")
                elif i == len(closed) - 1 and bear_enabled and bear_engulf:
                    emitted = self._signal("SHORT", row, None, float(row["high"]), "Bearish engulfing")
                if emitted:
                    break

        self.last_processed_time = self._time(closed.iloc[-1]["time"])
        return emitted

    def pending_snapshot(self):
        return self.manager.snapshot()

