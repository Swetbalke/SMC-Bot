"""
config_manager.py — Central Configuration Manager
Ek hi jagah se saari settings padho aur likhto.
Thread-safe atomic writes. Hot-reload support.
"""

import json
import hashlib
import os
import threading
import time
from copy import deepcopy
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config" / "bot_settings.json"
EMA_TRAIL_STRATEGY = "EMA Crossover + 100-Point Trail"
LEGACY_EMA_STRATEGY = "EMA 9/15 + Engulfing"
EMA_STRATEGY_NAMES = {EMA_TRAIL_STRATEGY, LEGACY_EMA_STRATEGY}

# Default fallback if file is missing or corrupt
DEFAULTS = {
    "general": {
        "strategy_name": "SMC BB Breakout",
        "symbol": "BTCUSDT",
        "sim_mode": True,
        "timezone": "Asia/Kolkata"
    },
    "execution": {
        "mode": "paper",
        "delta_mirror_enabled": False,
        "demo_trading_enabled": False,
        "fee_rate_per_side": 0.0005,
        "slippage_bps": 2.0
    },
    "data": {
        "source": "delta_public",
        "timeframe": "1m",
        "candle_history": 200,
        "fallback_csv": "data/btc_1m_sample.csv"
    },
    "indicators": {
        "bb_period": 20,
        "bb_std": 2.0,
        "rsi_period": 14,
        "ema_period": 50,
        "ema_fast_period": 9,
        "ema_slow_period": 15,
        "ema_crossover_enabled": False,
        "bullish_engulfing_enabled": False,
        "bearish_engulfing_enabled": False,
        "signal_confirmation_mode": "any",
        "ema_signal_mode": "sequence",
        "ema_sl_mode": "candle_extreme",
        "engulfing_wait_candles": 3,
        "sr_lookback": 20,
        "use_rsi": False,
        "use_ema": False,
        "use_macd": False
    },
    "entry": {
        "allow_long": True,
        "allow_short": True,
        "trade_direction": "both",
        "max_daily_trades": 5,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "rsi_long_max": 75,
        "rsi_short_min": 25
    },
    "exit": {
        "mandatory_stop_loss": True,
        "minimum_sl_points": 0.01,
        "sl_calculation_mode": "fixed_points",
        "sl_points": 50.0,
        "use_stop_loss": True,
        "use_take_profit": False,
        "sl_mode": "pct",
        "sl_value": 0.5,
        "tp_mode": "risk_reward",
        "tp_value": 1.5,
        "use_trailing_stop": True,
        "trailing_method": "rr",
        "trailing_update_frequency": "tick",
        "breakeven_trigger_rr": 2.0,
        "profit_lock_trigger_rr": 3.0,
        "profit_lock_level_rr": 2.0,
        "trailing_stop_points": 50.0,
        "trailing_stop_pct": 0.3,
        "atr_period": 14,
        "atr_multiplier": 2.0,
        "fee_adjust_breakeven": False
    },
    "risk": {
        "capital": 1000.0,
        "risk_pct": 2.0,
        "fixed_lot_size": 0.01,
        "minimum_position_size": 0.001,
        "maximum_position_size": 1.0,
        "position_size_mode": "risk_percent",
        "risk_reward_ratio": 3.0,
        "max_daily_loss_pct": 5.0,
        "max_daily_loss_amount": 0.0,
        "max_daily_profit_pct": 0.0,
        "max_open_positions": 1,
        "max_consecutive_losses": 0,
        "trading_session": "all_day",
        "trading_start_time": "00:00",
        "trading_end_time": "23:59"
    },
    "processing": {
        "analysis_interval_sec": 60,
        "position_check_interval_sec": 30,
        "log_max_bytes": 5242880,
        "log_backup_count": 3
    }
}


class ConfigManager:
    """
    Singleton config manager.
    Usage:
        from config_manager import config
        val = config.get("risk.capital")
        config.set("risk.capital", 1500.0)
    """
    _instance = None
    _lock = threading.RLock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._data = {}
            cls._instance._last_mtime = 0
            cls._instance._load()
        return cls._instance

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def get(self, dotpath: str, default=None):
        """Read a nested key using dot notation: 'risk.capital'"""
        with self._lock:
            self._reload_if_changed()
            keys = dotpath.split(".")
            val = self._data
            for k in keys:
                if isinstance(val, dict) and k in val:
                    val = val[k]
                else:
                    return default
            return val

    def get_section(self, section: str) -> dict:
        """Return a full section as a dict copy."""
        with self._lock:
            self._reload_if_changed()
            return deepcopy(self._data.get(section, {}))

    def get_all(self) -> dict:
        """Return full config copy."""
        with self._lock:
            self._reload_if_changed()
            return deepcopy(self._data)

    def fingerprint(self) -> str:
        """Stable identifier used by the engine to acknowledge applied settings."""
        with self._lock:
            self._reload_if_changed()
            payload = json.dumps(self._data, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def set(self, dotpath: str, value) -> bool:
        """Set a nested key and save atomically. Returns True on success."""
        with self._lock:
            self._reload_if_changed()
            keys = dotpath.split(".")
            d = self._data
            for k in keys[:-1]:
                if k not in d:
                    d[k] = {}
                d = d[k]
            d[keys[-1]] = value
            return self._save()

    def set_section(self, section: str, data: dict) -> bool:
        """Replace an entire section and save."""
        with self._lock:
            self._reload_if_changed()
            self._data[section] = deepcopy(data)
            return self._save()

    def update_all(self, data: dict) -> bool:
        """Validate and atomically replace the complete persisted configuration."""
        errors = self.validate(data)
        if errors:
            raise ValueError("; ".join(errors))
        with self._lock:
            self._reload_if_changed()
            candidate = deepcopy(DEFAULTS)
            self._deep_merge(candidate, data)
            previous = self._data
            self._data = candidate
            if self._save():
                return True
            self._data = previous
            return False

    @staticmethod
    def validate(data: dict) -> list:
        errors = []
        if not isinstance(data, dict):
            return ["Configuration must be an object"]
        try:
            ind, ent = data["indicators"], data["entry"]
            risk, ex, general, market = data["risk"], data["exit"], data["general"], data["data"]
            if int(ind["ema_fast_period"]) < 1 or int(ind["ema_slow_period"]) <= int(ind["ema_fast_period"]):
                errors.append("Slow EMA period must be greater than Fast EMA period")
            if market["timeframe"] not in {"1m", "3m", "5m", "15m", "30m", "1h", "4h"}:
                errors.append("Unsupported timeframe")
            if general.get("strategy_name") in EMA_STRATEGY_NAMES and not (
                ind.get("ema_crossover_enabled") or ind.get("bullish_engulfing_enabled") or ind.get("bearish_engulfing_enabled")
            ):
                errors.append("EMA strategy requires at least one enabled signal condition")
            if (general.get("strategy_name") in EMA_STRATEGY_NAMES
                    and ind.get("ema_signal_mode", "sequence") == "sequence"
                    and ind.get("ema_crossover_enabled")
                    and not ((ent.get("allow_long", True) and ind.get("bullish_engulfing_enabled"))
                             or (ent.get("allow_short", True) and ind.get("bearish_engulfing_enabled")))):
                errors.append("EMA sequence mode needs an engulfing confirmation enabled for an allowed trade direction; choose crossover_only for EMA-only entries")
            if general.get("strategy_name") in EMA_STRATEGY_NAMES:
                if (ent.get("trade_direction") != "both"
                        or not ent.get("allow_long") or not ent.get("allow_short")
                        or not ind.get("ema_crossover_enabled")
                        or ind.get("ema_signal_mode") != "crossover_only"
                        or ind.get("ema_sl_mode") != "fixed_points"
                        or float(ex.get("sl_points", 0)) != 100.0
                        or ex.get("use_take_profit")
                        or not ex.get("use_trailing_stop")
                        or ex.get("trailing_method") != "fixed_points"
                        or float(ex.get("trailing_stop_points", 0)) != 100.0):
                    errors.append("EMA Crossover + 100-Point Trail requires crossover-only entries, 100-point initial/trailing stops, and no fixed TP")
            if ent["trade_direction"] not in {"both", "buy_only", "sell_only"}:
                errors.append("Invalid trade direction")
            if data.get("execution", {}).get("mode", "paper") not in {"paper", "delta_demo"}:
                errors.append("Execution mode must be paper or delta_demo")
            if not isinstance(data.get("execution", {}).get("demo_trading_enabled", False), bool):
                errors.append("Demo trading enabled must be true or false")
            if not isinstance(data.get("execution", {}).get("delta_mirror_enabled", False), bool):
                errors.append("Delta mirror enabled must be true or false")
            if data.get("execution", {}).get("mode") == "delta_demo" and general.get("symbol") != "BTCUSD":
                errors.append("Delta Demo execution currently supports BTCUSD only")
            if risk["position_size_mode"] not in {"risk_percent", "fixed_lot"}:
                errors.append("Invalid position sizing mode")
            if risk.get("trading_session", "all_day") not in {"all_day", "custom"}:
                errors.append("Invalid trading session")
            for key in ("trading_start_time", "trading_end_time"):
                from datetime import datetime
                datetime.strptime(risk[key], "%H:%M")
            if ex["sl_mode"] not in {"pct", "fixed"} or ex["tp_mode"] not in {"pct", "fixed", "risk_reward"}:
                errors.append("Invalid stop-loss or take-profit mode")
            if ind.get("signal_confirmation_mode", "any") not in {"any", "all", "ema_only", "engulfing_only"}:
                errors.append("Invalid signal confirmation mode")
            if ind.get("ema_signal_mode", "sequence") not in {"sequence", "crossover_only", "engulfing_only"}:
                errors.append("Invalid EMA strategy confirmation mode")
            if ind.get("ema_sl_mode", "candle_extreme") not in {"candle_extreme", "fixed_points", "pct", "fixed"}:
                errors.append("Invalid EMA strategy stop-loss mode")
            if ex.get("sl_calculation_mode", "fixed_points") not in {"fixed_points", "engulfing_candle", "custom"}:
                errors.append("Invalid stop-loss calculation mode")
            if float(ex.get("sl_points", 50.0)) <= 0 or float(ex.get("minimum_sl_points", 0.01)) <= 0:
                errors.append("Stop-loss points and minimum distance must be greater than zero")
            if ex.get("trailing_method", "rr") not in {"rr", "fixed_points", "percentage", "atr"}:
                errors.append("Invalid trailing method")
            if ex.get("trailing_update_frequency", "tick") not in {"tick", "candle_close"}:
                errors.append("Invalid trailing update frequency")
            for key in ("breakeven_trigger_rr", "profit_lock_trigger_rr", "profit_lock_level_rr",
                        "trailing_stop_points", "trailing_stop_pct", "atr_multiplier"):
                if float(ex.get(key, 0)) <= 0:
                    errors.append(f"{key} must be greater than zero")
            if float(ex.get("profit_lock_trigger_rr", 3)) <= float(ex.get("breakeven_trigger_rr", 2)):
                errors.append("Profit-lock trigger must be greater than the breakeven trigger")
            if float(ex.get("profit_lock_level_rr", 2)) >= float(ex.get("profit_lock_trigger_rr", 3)):
                errors.append("Profit-lock level must be below its trigger")
            if int(ex.get("atr_period", 14)) < 2:
                errors.append("ATR period must be at least 2")
            if not isinstance(ex.get("mandatory_stop_loss", True), bool):
                errors.append("Mandatory stop-loss setting must be true or false")
            if ex.get("sl_calculation_mode") == "engulfing_candle" and general.get("strategy_name") not in EMA_STRATEGY_NAMES:
                errors.append("Engulfing-candle stop mode requires the EMA + Engulfing strategy")
            for section, keys in (("risk", ("risk_pct", "fixed_lot_size", "minimum_position_size", "maximum_position_size", "capital", "risk_reward_ratio")),
                                  ("entry", ("max_daily_trades",)), ("risk", ("max_open_positions",))):
                for key in keys:
                    if float(data[section][key]) <= 0:
                        errors.append(f"{key} must be greater than zero")
            for key in ("risk_pct", "fixed_lot_size", "capital"):
                if float(risk[key]) <= 0:
                    errors.append(f"{key} must be greater than zero")
            if float(risk["minimum_position_size"]) > float(risk["maximum_position_size"]):
                errors.append("Minimum position size cannot exceed maximum position size")
            if risk["position_size_mode"] == "fixed_lot" and not float(risk["minimum_position_size"]) <= float(risk["fixed_lot_size"]) <= float(risk["maximum_position_size"]):
                errors.append("Fixed lot size must be between configured minimum and maximum position size")
            if float(data.get("execution", {}).get("fee_rate_per_side", 0.0005)) < 0:
                errors.append("fee_rate_per_side cannot be negative")
            if float(data.get("execution", {}).get("slippage_bps", 2.0)) < 0:
                errors.append("slippage_bps cannot be negative")
            for key in ("max_daily_loss_amount", "max_daily_profit_pct", "max_consecutive_losses"):
                if float(risk.get(key, 0)) < 0:
                    errors.append(f"{key} cannot be negative")
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"Missing or invalid setting: {exc}")
        return errors

    def reload(self):
        """Force reload from disk."""
        with self._lock:
            self._load()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _load(self):
        """Load config from file, merge with defaults."""
        self._data = deepcopy(DEFAULTS)
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    file_data = json.load(f)
                self._deep_merge(self._data, file_data)
                # Preserve explicitly saved legacy SL behavior until the user
                # selects one of the new modes in Settings.
                saved_exit = file_data.get("exit", {}) if isinstance(file_data, dict) else {}
                if "sl_calculation_mode" not in saved_exit and "sl_mode" in saved_exit:
                    self._data["exit"]["sl_calculation_mode"] = "custom"
                if "trailing_method" not in saved_exit and saved_exit.get("use_trailing_stop"):
                    self._data["exit"]["trailing_method"] = "percentage"
                self._last_mtime = CONFIG_PATH.stat().st_mtime
        except Exception as e:
            print(f"[ConfigManager] WARNING: Could not load config: {e}. Using defaults.")

    def _reload_if_changed(self):
        """Auto-reload if file was modified on disk (hot-reload for engine)."""
        try:
            if CONFIG_PATH.exists():
                mtime = CONFIG_PATH.stat().st_mtime
                if mtime != self._last_mtime:
                    self._load()
        except Exception:
            pass

    def _save(self) -> bool:
        """Atomic write: write to temp file then rename."""
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=4, ensure_ascii=False)
            tmp.replace(CONFIG_PATH)
            self._last_mtime = CONFIG_PATH.stat().st_mtime
            return True
        except Exception as e:
            print(f"[ConfigManager] ERROR: Could not save config: {e}")
            return False

    def _deep_merge(self, base: dict, override: dict):
        """Recursively merge override into base (in-place)."""
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._deep_merge(base[k], v)
            else:
                base[k] = v


# Module-level singleton — import this everywhere
config = ConfigManager()


if __name__ == "__main__":
    print("=== ConfigManager Test ===")
    print(f"Capital: {config.get('risk.capital')}")
    print(f"Symbol:  {config.get('general.symbol')}")
    print(f"SL Mode: {config.get('exit.sl_mode')}")
    config.set("risk.capital", 1500.0)
    print(f"Capital after set: {config.get('risk.capital')}")
    print("Test PASSED.")
