"""Deterministic protective-stop, RR trailing, and paper persistence checks."""
import sys
import tempfile
import unittest
import importlib.util
import sqlite3
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import database as db
from config_manager import DEFAULTS, ConfigManager
from paper_execution import PaperExecutionAdapter
from strategies.stop_manager import calculate_initial_stop, propose_trailing_stop, validate_stop

# The stop/DB unit tests do not call dataframe or vectorized operations. A
# lightweight import shim lets those focused tests run when optional project
# dependencies are absent; full app/strategy tests still need requirements.txt.
if importlib.util.find_spec("pandas") is None or importlib.util.find_spec("numpy") is None:
    pd_stub = types.ModuleType("pandas")
    pd_stub.DataFrame = type("DataFrame", (), {})
    pd_stub.Series = type("Series", (), {})
    sys.modules.setdefault("pandas", pd_stub)
    np_stub = types.ModuleType("numpy")
    np_stub.nan = float("nan")
    np_stub.isfinite = lambda value: True
    sys.modules.setdefault("numpy", np_stub)
if importlib.util.find_spec("requests") is None:
    sys.modules.setdefault("requests", types.ModuleType("requests"))
import paper_engine


class StopManagementTests(unittest.TestCase):
    def make_position(self, direction):
        long_side = direction == "LONG"
        return {
            "id": 1, "direction": direction, "entry_price": 100.0,
            "initial_sl_price": 95.0 if long_side else 105.0,
            "sl_price": 95.0 if long_side else 105.0,
            "initial_risk_points": 5.0, "best_favorable_price": 100.0,
            "trailing_method": "rr", "trailing_enabled": 1,
            "trailing_status": "active", "breakeven_trigger_rr": 2.0,
            "profit_lock_trigger_rr": 3.0, "profit_lock_level_rr": 2.0,
            "fee_adjust_breakeven": 0, "entry_config": {},
        }

    def test_initial_fixed_and_candle_stops_both_directions(self):
        self.assertEqual(calculate_initial_stop("LONG", 100, "fixed_points", 50), 50)
        self.assertEqual(calculate_initial_stop("SHORT", 100, "fixed_points", 50), 150)
        self.assertEqual(calculate_initial_stop("LONG", 100, "engulfing_candle", signal_stop=97), 97)
        self.assertEqual(calculate_initial_stop("SHORT", 100, "engulfing_candle", signal_stop=103), 103)

    def test_invalid_stops_are_rejected(self):
        self.assertIsNotNone(validate_stop("LONG", 100, 100))
        self.assertIsNotNone(validate_stop("SHORT", 100, 0))
        self.assertIsNotNone(validate_stop("LONG", 100, 99.995, 0.01))
        adapter = PaperExecutionAdapter()
        self.assertFalse(adapter.confirm_entry_stop("LONG", 100, 100)["accepted"])
        self.assertFalse(adapter.confirm_stop_update(
            {"direction": "LONG", "sl_price": 98}, 97)["accepted"])

    def test_long_rr_milestones_and_no_backward_move(self):
        pos = self.make_position("LONG")
        at_1r = propose_trailing_stop(pos, 105)
        self.assertEqual(at_1r["stop_price"], 95)
        self.assertEqual(at_1r["rr"], 1)
        pos["best_favorable_price"] = 105
        at_2r = propose_trailing_stop(pos, 110)
        self.assertEqual(at_2r["stop_price"], 100)
        pos.update(sl_price=100, best_favorable_price=110)
        at_3r = propose_trailing_stop(pos, 115)
        self.assertEqual(at_3r["stop_price"], 110)
        pos.update(sl_price=110, best_favorable_price=115)
        at_4r = propose_trailing_stop(pos, 120)
        self.assertEqual(at_4r["stop_price"], 115)
        pos.update(sl_price=115, best_favorable_price=120)
        reverse = propose_trailing_stop(pos, 114)
        self.assertEqual(reverse["stop_price"], 115)

    def test_short_rr_milestones_and_no_backward_move(self):
        pos = self.make_position("SHORT")
        self.assertEqual(propose_trailing_stop(pos, 95)["stop_price"], 105)
        pos["best_favorable_price"] = 95
        self.assertEqual(propose_trailing_stop(pos, 90)["stop_price"], 100)
        pos.update(sl_price=100, best_favorable_price=90)
        self.assertEqual(propose_trailing_stop(pos, 85)["stop_price"], 90)
        pos.update(sl_price=90, best_favorable_price=85)
        self.assertEqual(propose_trailing_stop(pos, 80)["stop_price"], 85)
        pos.update(sl_price=85, best_favorable_price=80)
        self.assertEqual(propose_trailing_stop(pos, 86)["stop_price"], 85)

    def test_fee_adjusted_breakeven(self):
        pos = self.make_position("LONG")
        pos["fee_adjust_breakeven"] = 1
        pos["entry_config"] = {"fee_rate_per_side": 0.001, "slippage_bps": 2}
        result = propose_trailing_stop(pos, 110)
        self.assertGreater(result["stop_price"], 100)

    def test_alternative_trailing_methods(self):
        pos = self.make_position("LONG")
        pos.update(trailing_method="fixed_points", trailing_stop_points=3,
                   best_favorable_price=110, sl_price=100)
        self.assertEqual(propose_trailing_stop(pos, 110)["stop_price"], 107)
        pos.update(trailing_method="percentage", trailing_stop_pct=1,
                   best_favorable_price=110, sl_price=100)
        self.assertEqual(propose_trailing_stop(pos, 110)["stop_price"], 108.9)
        pos.update(trailing_method="atr", atr_multiplier=2,
                   best_favorable_price=110, sl_price=100)
        self.assertEqual(propose_trailing_stop(pos, 110, atr_value=2)["stop_price"], 106)
        self.assertEqual(propose_trailing_stop(pos, 110, atr_value=None)["stop_price"], 100)

    def test_one_to_one_hundred_point_trail_long_and_short(self):
        long_pos = {
            "direction": "LONG", "entry_price": 1000.0,
            "initial_sl_price": 900.0, "initial_risk_points": 100.0,
            "sl_price": 900.0, "best_favorable_price": 1000.0,
            "trailing_method": "fixed_points", "trailing_stop_points": 100.0,
            "rr_milestones_enabled": False,
        }
        self.assertEqual(propose_trailing_stop(long_pos, 1005.0)["stop_price"], 905.0)
        self.assertEqual(propose_trailing_stop(long_pos, 1100.0)["stop_price"], 1000.0)
        short_pos = {
            "direction": "SHORT", "entry_price": 1000.0,
            "initial_sl_price": 1100.0, "initial_risk_points": 100.0,
            "sl_price": 1100.0, "best_favorable_price": 1000.0,
            "trailing_method": "fixed_points", "trailing_stop_points": 100.0,
            "rr_milestones_enabled": False,
        }
        self.assertEqual(propose_trailing_stop(short_pos, 995.0)["stop_price"], 1095.0)
        self.assertEqual(propose_trailing_stop(short_pos, 900.0)["stop_price"], 1000.0)

    def test_config_validation_for_stop_settings(self):
        import copy
        cfg = copy.deepcopy(DEFAULTS)
        self.assertEqual(ConfigManager.validate(cfg), [])
        cfg["exit"]["mandatory_stop_loss"] = False
        self.assertEqual(ConfigManager.validate(cfg), [])
        cfg["exit"]["sl_points"] = 0
        self.assertTrue(ConfigManager.validate(cfg))
        cfg = copy.deepcopy(DEFAULTS)
        cfg["exit"]["profit_lock_trigger_rr"] = 1.5
        self.assertTrue(ConfigManager.validate(cfg))

    def test_legacy_config_keeps_existing_sl_and_trailing_method(self):
        import json
        import config_manager as config_module
        original_path = config_module.CONFIG_PATH
        temp_dir = Path(tempfile.gettempdir()) / "tradingbot_legacy_config_test"
        temp_dir.mkdir(exist_ok=True)
        config_path = temp_dir / "bot_settings.json"
        saved = {"exit": {"sl_mode": "pct", "sl_value": 0.5,
                          "use_trailing_stop": True, "trailing_stop_pct": 0.3}}
        config_path.write_text(json.dumps(saved), encoding="utf-8")
        try:
            config_module.CONFIG_PATH = config_path
            cm = object.__new__(ConfigManager)
            cm._data = {}
            cm._last_mtime = 0
            cm._load()
            self.assertEqual(cm.get("exit.sl_calculation_mode"), "custom")
            self.assertEqual(cm.get("exit.sl_mode"), "pct")
            self.assertEqual(cm.get("exit.sl_value"), 0.5)
            self.assertEqual(cm.get("exit.trailing_method"), "percentage")
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), saved)
        finally:
            config_module.CONFIG_PATH = original_path
            config_path.unlink(missing_ok=True)
            temp_dir.rmdir()

    def test_persisted_open_settings_and_restart_recovery(self):
        original_path = db.DB_PATH
        temp_path = Path(tempfile.gettempdir()) / "tradingbot_stop_management_test.db"
        for suffix in ("", "-wal", "-shm"):
            Path(str(temp_path) + suffix).unlink(missing_ok=True)
        db.DB_PATH = temp_path
        try:
            db.init_db()
            trailing = dict(DEFAULTS["exit"])
            pos_id = db.save_position("BTCUSDT", "LONG", 100, 0.01, 95, 0,
                "2026-01-01 10:00:00", sl_enabled=True, tp_enabled=False,
                trailing_enabled=True, initial_sl_price=95, initial_risk_points=5,
                trailing_settings=trailing,
                entry_config={"fee_rate_per_side": 0.0005, "slippage_bps": 2, "timeframe": "1m"})
            other_id = db.save_position("ETHUSDT", "SHORT", 200, 0.01, 205, 0,
                "2026-01-01 10:00:01", sl_enabled=True, tp_enabled=False,
                trailing_enabled=True, initial_sl_price=205, initial_risk_points=5,
                trailing_settings=trailing, entry_config={"timeframe": "1m"})
            pos = db.get_open_positions()[0]
            self.assertEqual(pos["id"], pos_id)
            self.assertEqual(pos["entry_config"]["timeframe"], "1m")
            self.assertTrue(db.update_position_stop(pos_id, 100, 110, 2, "breakeven"))
            self.assertFalse(db.update_position_stop(pos_id, 99, 110, 2, "active"))
            recovered = db.get_open_positions()[0]
            self.assertEqual(recovered["sl_price"], 100)
            self.assertEqual(recovered["initial_sl_price"], 95)
            self.assertEqual(recovered["trailing_status"], "breakeven")
            other = next(p for p in db.get_open_positions() if p["id"] == other_id)
            self.assertEqual(other["sl_price"], 205)
        finally:
            db.DB_PATH = original_path
            for suffix in ("", "-wal", "-shm"):
                Path(str(temp_path) + suffix).unlink(missing_ok=True)

    def test_paper_engine_initial_50_point_stop_and_confirmation_failure(self):
        original_path = db.DB_PATH
        temp_path = Path(tempfile.gettempdir()) / "tradingbot_stop_entry_test.db"
        for suffix in ("", "-wal", "-shm"):
            Path(str(temp_path) + suffix).unlink(missing_ok=True)
        db.DB_PATH = temp_path
        exit_cfg = dict(DEFAULTS["exit"])
        exit_cfg.update(sl_calculation_mode="fixed_points", sl_points=50,
                        use_take_profit=False, use_trailing_stop=True)
        risk_cfg = dict(DEFAULTS["risk"], position_size_mode="fixed_lot", fixed_lot_size=0.01)
        execution_cfg = {"fee_rate_per_side": 0.0, "slippage_bps": 0.0}
        def get_section(section):
            return {"exit": exit_cfg, "risk": risk_cfg, "execution": execution_cfg}[section]
        def get_value(path, default=None):
            return {"general.symbol": "BTCUSDT", "general.strategy_name": "SMC BB Breakout",
                    "data.timeframe": "1m"}.get(path, default)
        try:
            db.init_db()
            engine = paper_engine.PaperEngine()
            with patch.object(paper_engine.config, "get_section", side_effect=get_section), \
                 patch.object(paper_engine.config, "get", side_effect=get_value):
                engine._open_position("LONG", 1000, "paper test", 1000,
                                      "2026-01-01 10:00:00", signal_id="fixed-sl-test")
                pos = db.get_open_positions()[0]
                self.assertEqual(pos["sl_price"], 950)
                self.assertEqual(pos["initial_sl_price"], 950)
                self.assertEqual(pos["tp_enabled"], 0)
                self.assertEqual(pos["trailing_method"], "rr")
                engine._open_position("SHORT", 1000, "paper sell test", 1000,
                                      "2026-01-01 10:00:30", signal_id="fixed-short-sl-test")
                short_pos = next(p for p in db.get_open_positions() if p["direction"] == "SHORT")
                self.assertEqual(short_pos["sl_price"], 1050)
                exit_cfg["mandatory_stop_loss"] = False
                engine._open_position("LONG", 1000, "locked entry", 1000,
                                      "2026-01-01 10:01:00", signal_id="mandatory-off-test")
                self.assertEqual(len(db.get_open_positions()), 2)
                exit_cfg["mandatory_stop_loss"] = True
                class RejectingAdapter:
                    @staticmethod
                    def confirm_entry_stop(*args, **kwargs):
                        return {"accepted": False, "error": "simulated rejection"}
                engine._execution = RejectingAdapter()
                engine._open_position("SHORT", 1000, "should be rejected", 1000,
                                      "2026-01-01 10:02:00", signal_id="reject-sl-test")
                self.assertEqual(len(db.get_open_positions()), 2)
                self.assertFalse(db.has_signal_been_traded("reject-sl-test"))
        finally:
            db.DB_PATH = original_path
            for suffix in ("", "-wal", "-shm"):
                Path(str(temp_path) + suffix).unlink(missing_ok=True)

    def test_paper_engine_trailing_milestones_and_stop_exit(self):
        original_path = db.DB_PATH
        temp_path = Path(tempfile.gettempdir()) / "tradingbot_stop_exit_test.db"
        for suffix in ("", "-wal", "-shm"):
            Path(str(temp_path) + suffix).unlink(missing_ok=True)
        db.DB_PATH = temp_path
        try:
            db.init_db()
            settings = dict(DEFAULTS["exit"])
            pos_id = db.save_position("BTCUSDT", "LONG", 100, 0.01, 95, 0,
                "2026-01-01 10:00:00", tp_enabled=False, trailing_enabled=True,
                initial_sl_price=95, initial_risk_points=5, trailing_settings=settings,
                entry_config={"fee_rate_per_side": 0.0, "slippage_bps": 0.0})
            engine = paper_engine.PaperEngine()
            one_r = db.get_open_positions()[0]
            engine._check_exit(one_r, 105)
            self.assertEqual(db.get_open_positions()[0]["sl_price"], 95)
            two_r = db.get_open_positions()[0]
            engine._check_exit(two_r, 110)
            self.assertEqual(db.get_open_positions()[0]["sl_price"], 100)
            three_r = db.get_open_positions()[0]
            engine._check_exit(three_r, 115)
            self.assertEqual(db.get_open_positions()[0]["sl_price"], 110)
            four_r = db.get_open_positions()[0]
            engine._check_exit(four_r, 120)
            self.assertEqual(db.get_open_positions()[0]["sl_price"], 115)
            reverse = db.get_open_positions()[0]
            engine._check_exit(reverse, 114)
            self.assertEqual(db.get_open_positions(), [])
            self.assertEqual(db.get_all_trades()[0]["exit_reason"], "SL Hit")
            self.assertEqual(db.get_all_trades()[0]["id"], pos_id)
        finally:
            db.DB_PATH = original_path
            for suffix in ("", "-wal", "-shm"):
                Path(str(temp_path) + suffix).unlink(missing_ok=True)

    def test_candle_close_frequency_only_updates_once_per_closed_bar(self):
        original_path = db.DB_PATH
        temp_path = Path(tempfile.gettempdir()) / "tradingbot_candle_trailing_test.db"
        for suffix in ("", "-wal", "-shm"):
            Path(str(temp_path) + suffix).unlink(missing_ok=True)
        db.DB_PATH = temp_path
        try:
            db.init_db()
            settings = dict(DEFAULTS["exit"], trailing_update_frequency="candle_close")
            db.save_position("BTCUSDT", "LONG", 100, 0.01, 95, 0,
                "2026-01-01 10:00:00", tp_enabled=False, trailing_enabled=True,
                initial_sl_price=95, initial_risk_points=5, trailing_settings=settings,
                entry_config={"fee_rate_per_side": 0.0, "slippage_bps": 0.0})
            engine = paper_engine.PaperEngine()
            pos = db.get_open_positions()[0]
            candle_time = "2026-01-01T10:01:00+00:00"
            engine._check_exit(pos, 110, candle_close=True, candle_time=candle_time)
            updated = db.get_open_positions()[0]
            self.assertEqual(updated["sl_price"], 100)
            self.assertEqual(updated["trailing_last_candle_time"], candle_time)
            engine._check_exit(updated, 115, candle_close=False, candle_time=candle_time)
            self.assertEqual(db.get_open_positions()[0]["sl_price"], 100)
        finally:
            db.DB_PATH = original_path
            for suffix in ("", "-wal", "-shm"):
                Path(str(temp_path) + suffix).unlink(missing_ok=True)

    def test_old_database_positions_migrate_without_loss(self):
        original_path = db.DB_PATH
        temp_path = Path(tempfile.gettempdir()) / "tradingbot_stop_migration_test.db"
        for suffix in ("", "-wal", "-shm"):
            Path(str(temp_path) + suffix).unlink(missing_ok=True)
        db.DB_PATH = temp_path
        try:
            conn = sqlite3.connect(str(temp_path))
            conn.execute("""CREATE TABLE open_positions (
                id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, direction TEXT NOT NULL,
                entry_price REAL NOT NULL, quantity REAL NOT NULL, sl_price REAL NOT NULL,
                tp_price REAL NOT NULL, entry_time TEXT NOT NULL, signal_reason TEXT,
                sl_enabled INTEGER NOT NULL DEFAULT 0, tp_enabled INTEGER NOT NULL DEFAULT 0,
                signal_id TEXT, trailing_enabled INTEGER NOT NULL DEFAULT 1,
                trailing_stop_pct REAL NOT NULL DEFAULT 0.3)""")
            conn.execute("""INSERT INTO open_positions
                (symbol,direction,entry_price,quantity,sl_price,tp_price,entry_time,sl_enabled,trailing_enabled)
                VALUES ('BTCUSDT','LONG',100,0.01,95,0,'2026-01-01 10:00:00',0,1)""")
            conn.execute("UPDATE open_positions SET trailing_stop_pct=10 WHERE id=1")
            conn.commit()
            conn.close()
            db.init_db()
            migrated = db.get_open_positions()[0]
            self.assertEqual(migrated["initial_sl_price"], 95)
            self.assertEqual(migrated["initial_risk_points"], 5)
            self.assertEqual(migrated["sl_enabled"], 1)
            self.assertEqual(migrated["rr_milestones_enabled"], 0)
            self.assertEqual(migrated["trailing_method"], "percentage")
            self.assertEqual(migrated["trailing_status"], "active")
            legacy_trail = propose_trailing_stop(migrated, 110)
            self.assertEqual(legacy_trail["stop_price"], 99)
        finally:
            db.DB_PATH = original_path
            for suffix in ("", "-wal", "-shm"):
                Path(str(temp_path) + suffix).unlink(missing_ok=True)

    def test_safe_stop_management_defaults(self):
        self.assertTrue(DEFAULTS["exit"]["mandatory_stop_loss"])
        self.assertEqual(DEFAULTS["exit"]["sl_points"], 50.0)
        self.assertTrue(DEFAULTS["exit"]["use_trailing_stop"])
        self.assertFalse(DEFAULTS["exit"]["use_take_profit"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
