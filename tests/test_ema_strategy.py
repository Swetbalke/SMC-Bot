"""Deterministic strategy, level, backtest and isolated paper-execution checks."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import database as db
import paper_engine
from backtest import run_ema_backtest
from strategies.ema_calculator import calculate_emas
from strategies.ema_engulfing_strategy import EMAEngulfingStrategy
from strategies.trade_levels import calculate_trade_levels


BASE_CONFIG = {
    "general": {"strategy_name": "EMA 9/15 + Engulfing", "symbol": "BTCUSDT"},
    "data": {"timeframe": "1m"},
    "indicators": {
        "ema_fast_period": 9, "ema_slow_period": 15,
        "ema_crossover_enabled": True, "bullish_engulfing_enabled": True,
        "bearish_engulfing_enabled": True, "engulfing_wait_candles": 3,
        "ema_signal_mode": "sequence", "ema_sl_mode": "candle_extreme",
    },
    "entry": {"trade_direction": "both", "max_daily_trades": 50},
    "risk": {"capital": 1000.0, "risk_pct": 1.0, "position_size_mode": "risk_percent",
             "fixed_lot_size": 0.01, "max_open_positions": 1},
    "exit": {"use_stop_loss": True, "use_take_profit": True, "sl_mode": "pct",
             "sl_value": 0.5, "tp_mode": "risk_reward", "risk_reward_ratio": 2.0,
             "tp_value": 1.0, "use_trailing_stop": False},
    "execution": {"fee_rate_per_side": 0.0005, "slippage_bps": 2.0},
}


def rising_setup(forming=True):
    closes = [120 - i for i in range(30)] + [91 + j * 4 for j in range(1, 7)]
    rows = []
    start = pd.Timestamp("2026-01-01", tz="UTC")
    for i, close in enumerate(closes):
        open_ = close + 1.0
        if i == 35:  # bullish body engulfs bearish crossover candle's real body
            open_ = closes[34] - 1.0
        rows.append({"time": start + pd.Timedelta(minutes=i), "open": open_,
                     "high": max(open_, close) + 1.0, "low": min(open_, close) - 1.0,
                     "close": float(close), "volume": 1.0})
    if forming:
        i = len(rows)
        rows.append({"time": start + pd.Timedelta(minutes=i), "open": closes[-1],
                     "high": closes[-1] + 1, "low": closes[-1] - 1,
                     "close": closes[-1], "volume": 1.0})
    return pd.DataFrame(rows)


def falling_setup(forming=True):
    closes = [80 + i for i in range(30)] + [111 - j * 4 for j in range(1, 7)]
    rows = []
    start = pd.Timestamp("2026-01-02", tz="UTC")
    for i, close in enumerate(closes):
        open_ = close - 1.0
        if i == 35:  # bearish body engulfs bullish crossover candle's real body
            open_ = closes[34] + 1.0
        rows.append({"time": start + pd.Timedelta(minutes=i), "open": open_,
                     "high": max(open_, close) + 1.0, "low": min(open_, close) - 1.0,
                     "close": float(close), "volume": 1.0})
    if forming:
        i = len(rows)
        rows.append({"time": start + pd.Timedelta(minutes=i), "open": closes[-1],
                     "high": closes[-1] + 1, "low": closes[-1] - 1,
                     "close": closes[-1], "volume": 1.0})
    return pd.DataFrame(rows)


def strat_config(**overrides):
    cfg = dict(BASE_CONFIG["indicators"])
    cfg.update(trade_direction="both")
    cfg.update(overrides)
    return cfg


class EMAEngulfingTests(unittest.TestCase):
    def test_bullish_crossover_then_closed_engulfing(self):
        strat = EMAEngulfingStrategy(strat_config())
        signal = strat.process(rising_setup(), "1m", True)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "LONG")
        self.assertLess(signal.stop_loss, signal.entry_reference)

    def test_bearish_crossover_then_closed_engulfing(self):
        strat = EMAEngulfingStrategy(strat_config())
        signal = strat.process(falling_setup(), "1m", True)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "SHORT")
        self.assertGreater(signal.stop_loss, signal.entry_reference)

    def test_cross_without_engulf_waits_but_does_not_enter(self):
        df = rising_setup()
        # Remove the only engulfing body while preserving all closes and cross logic.
        df.loc[35, "open"] = df.loc[35, "close"] + 1
        strat = EMAEngulfingStrategy(strat_config())
        self.assertIsNone(strat.process(df, "1m", True))
        self.assertIsNotNone(strat.pending_snapshot())

    def test_sequence_mode_never_falls_back_to_cross_without_confirmation(self):
        df = rising_setup(forming=False)
        cfg = strat_config(bullish_engulfing_enabled=False, bearish_engulfing_enabled=False)
        signal = EMAEngulfingStrategy(cfg).process(df.iloc[:35], "1m", False)
        self.assertIsNone(signal)

    def test_crossover_only_emits_without_engulfing(self):
        df = rising_setup(forming=False)
        cfg = strat_config(ema_signal_mode="crossover_only",
                           bullish_engulfing_enabled=False, bearish_engulfing_enabled=False)
        signal = EMAEngulfingStrategy(cfg).process(df.iloc[:35], "1m", False)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "LONG")
        self.assertEqual(signal.reason, "EMA crossover")

    def test_engulf_without_cross_is_not_a_sequence_signal(self):
        df = rising_setup()
        df.loc[34:, "close"] = [100.0, 102.0, 102.0]
        df.loc[34, "open"] = 101.0
        df.loc[35, "open"] = 99.0
        strat = EMAEngulfingStrategy(strat_config())
        self.assertIsNone(strat.process(df, "1m", True))

    def test_opposite_cross_invalidates_pending_setup(self):
        df = rising_setup(forming=False)
        df.loc[35, "open"] = df.loc[35, "close"] + 1.0
        strat = EMAEngulfingStrategy(strat_config(engulfing_wait_candles=20))
        self.assertIsNone(strat.process(df.iloc[:35], "1m", False))
        start = df.iloc[-1]["time"]
        closes = [100, 95, 90, 85, 80, 75, 70, 65, 60]
        rows = []
        for j, close in enumerate(closes, 1):
            open_ = close + 1.0
            rows.append({"time": start + pd.Timedelta(minutes=j), "open": open_,
                         "high": open_ + 1, "low": close - 1, "close": float(close), "volume": 1})
        df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
        self.assertIsNone(strat.process(df, "1m", False))
        pending = strat.pending_snapshot()
        self.assertTrue(pending is None or pending["direction"] == "SHORT")

    def test_missing_candle_gap_clears_pending(self):
        df = rising_setup(forming=False)
        df.loc[35, "open"] = df.loc[35, "close"] + 1.0
        strat = EMAEngulfingStrategy(strat_config(engulfing_wait_candles=20))
        self.assertIsNone(strat.process(df.iloc[:35], "1m", False))
        last = df.iloc[34]
        gap_bar = {"time": last["time"] + pd.Timedelta(minutes=4), "open": 130.0,
                   "high": 131.0, "low": 129.0, "close": 130.0, "volume": 1.0}
        gap = pd.concat([df.iloc[:35], pd.DataFrame([gap_bar])], ignore_index=True)
        self.assertIsNone(strat.process(gap, "1m", False))
        self.assertIsNone(strat.pending_snapshot())

    def test_expired_setup_and_duplicate_signal(self):
        strat = EMAEngulfingStrategy(strat_config())
        df = rising_setup()
        signal = strat.process(df, "1m", True)
        self.assertIsNotNone(signal)
        self.assertIsNone(strat.process(df, "1m", True))
        no_pattern = rising_setup(forming=False)
        no_pattern.loc[35, "open"] = no_pattern.loc[35, "close"] + 1.0
        # Append enough later candles that the original setup expires.
        tail = []
        start = no_pattern.iloc[-1]["time"]
        for j, c in enumerate([116.0, 117.0, 118.0, 119.0], 1):
            tail.append({"time": start + pd.Timedelta(minutes=j), "open": c + 1,
                         "high": c + 2, "low": c - 1, "close": c, "volume": 1})
        delayed = pd.concat([no_pattern, pd.DataFrame(tail)], ignore_index=True)
        delayed.loc[len(delayed) - 2, "open"] = delayed.iloc[-2]["close"] - 2
        strat = EMAEngulfingStrategy(strat_config(engulfing_wait_candles=1))
        self.assertIsNone(strat.process(no_pattern, "1m", False))
        for end in range(len(no_pattern) + 1, len(delayed) + 1):
            self.assertIsNone(strat.process(delayed.iloc[:end], "1m", end == len(delayed)))

    def test_incomplete_candle_missing_data_and_direction_filter(self):
        df = rising_setup()
        cfg = strat_config(trade_direction="sell_only")
        self.assertIsNone(EMAEngulfingStrategy(cfg).process(df, "1m", True))
        # Treat the final closed-pattern bar as still forming; it cannot confirm yet.
        self.assertIsNone(EMAEngulfingStrategy(strat_config()).process(df.iloc[:-1], "1m", True))
        self.assertIsNone(EMAEngulfingStrategy(strat_config()).process(pd.DataFrame(), "1m", True))
        malformed = df.copy()
        malformed.loc[35, "high"] = malformed.loc[35, "low"] - 1
        self.assertIsNone(EMAEngulfingStrategy(strat_config()).process(malformed, "1m", True))

    def test_levels_rr_and_invalid_directional_stop(self):
        sl, tp = calculate_trade_levels("LONG", 100, 98, BASE_CONFIG["exit"])
        self.assertEqual((sl, tp), (98.0, 104.0))
        sl, tp = calculate_trade_levels("SHORT", 100, 102, BASE_CONFIG["exit"])
        self.assertEqual((sl, tp), (102.0, 96.0))
        with self.assertRaises(ValueError):
            calculate_trade_levels("LONG", 100, 101, BASE_CONFIG["exit"])

    def test_backtest_uses_next_open_and_costs(self):
        df = rising_setup(forming=False)
        next_bar = {"time": df.iloc[-1]["time"] + pd.Timedelta(minutes=1), "open": 116.0,
                    "high": 140.0, "low": 115.0, "close": 138.0, "volume": 1.0}
        df = pd.concat([df, pd.DataFrame([next_bar])], ignore_index=True)
        result = run_ema_backtest(df, BASE_CONFIG)
        self.assertEqual(result["total_trades"], 1)
        self.assertEqual(result["trades"][0]["exit_reason"], "TP Hit")
        self.assertGreater(result["total_trading_costs"], 0)
        self.assertGreater(result["trades"][0]["entry_price"], next_bar["open"])

    def test_isolated_paper_trade_open_and_close(self):
        original_path = db.DB_PATH
        temp_path = Path(tempfile.gettempdir()) / "tradingbot_task1_isolated_test.db"
        temp_path.unlink(missing_ok=True)
        db.DB_PATH = temp_path
        try:
            db.init_db()
            engine = paper_engine.PaperEngine()
            risk_pct = [1.0]
            def test_section(key):
                if key == "execution":
                    return {"slippage_bps": 0.0, "fee_rate_per_side": 0.0005}
                if key == "risk":
                    return dict(BASE_CONFIG["risk"], risk_pct=risk_pct[0])
                return dict(BASE_CONFIG.get(key, {}))
            with patch.object(paper_engine.config, "get_section", side_effect=test_section), \
                 patch.object(paper_engine.config, "get", side_effect=lambda key, default=None: {
                     "general.symbol": "BTCUSDT", "indicators.ema_sl_mode": "candle_extreme",
                     "execution.slippage_bps": 0.0, "execution.fee_rate_per_side": 0.0005,
                     "exit.trailing_stop_pct": 0.3,
                 }.get(key, default)):
                engine._open_position("LONG", 10000.0, "test crossover+engulf", 1000.0,
                                      "2026-01-01 10:00:00", signal_stop=9800.0,
                                      signal_id="isolated-demo-trade-1")
                opened = db.get_open_positions()
                self.assertEqual(len(opened), 1)
                self.assertEqual(opened[0]["sl_price"], 9800.0)
                self.assertEqual(opened[0]["tp_price"], 10400.0)
                self.assertAlmostEqual(opened[0]["quantity"], 0.0476, places=4)
                self.assertTrue(db.has_signal_been_traded("isolated-demo-trade-1"))
                engine._check_exit(opened[0], 10500.0)
                self.assertEqual(len(db.get_open_positions()), 0)
                trades = db.get_all_trades()
                self.assertEqual(len(trades), 1)
                self.assertEqual(trades[0]["exit_reason"], "TP Hit")
                self.assertEqual(trades[0]["signal_id"], "isolated-demo-trade-1")
                self.assertTrue(db.has_signal_been_traded("isolated-demo-trade-1"))
                risk_pct[0] = 0.5
                engine._open_position("LONG", 10000.0, "updated risk setting", 1000.0,
                                      "2026-01-01 10:05:00", signal_stop=9800.0,
                                      signal_id="isolated-demo-trade-2")
                second = db.get_open_positions()[0]
                self.assertAlmostEqual(second["quantity"], 0.0238, places=4)
                engine._check_exit(second, 9700.0)
                self.assertEqual(len(db.get_open_positions()), 0)
                second_trade = next(t for t in db.get_all_trades() if t["signal_id"] == "isolated-demo-trade-2")
                self.assertEqual(second_trade["exit_reason"], "SL Hit")
        finally:
            db.DB_PATH = original_path


if __name__ == "__main__":
    unittest.main(verbosity=2)

