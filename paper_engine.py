"""
paper_engine.py — Paper Trading and Delta Demo Engine
Paper mode simulates trades; Delta Demo mode submits BTCUSD testnet orders.
Credentials are read from process environment and never written to config.
"""

import sys
import os
import json
import time
import signal
import threading
import math
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional
import pandas as pd

# --- Project imports ---
from config_manager import config, EMA_STRATEGY_NAMES
from logger_setup import get_logger
from database import (
    init_db, get_state, update_state, check_and_reset_daily,
    save_position, get_open_positions, close_position,
    record_trade, log_signal, log_event, get_performance_summary, get_all_trades,
    update_strategy_runtime, has_signal_been_traded,
    update_position_stop, update_trailing_candle_time, update_exchange_status,
    get_exchange_status
)
from data_client import DeltaPublicClient, generate_sample_csv
from strategy import detect_signals, calc_sl_tp, calc_quantity
from indicators import apply_all_indicators
from strategies.ema_engulfing_strategy import EMAEngulfingStrategy
from strategies.stop_manager import calculate_initial_stop, validate_stop, propose_trailing_stop
from paper_execution import PaperExecutionAdapter
from delta_exchange import DeltaDemoClient, DeltaAPIError
from excel_exporter import _excel_exporter

logger = get_logger("paper_engine")

# ====================================================================== #
# Single-instance lock (prevent two engines running simultaneously)
# ====================================================================== #

LOCKFILE = Path(__file__).parent / "data" / "engine.lock"


def acquire_lock() -> bool:
    """Create lock file with PID. Returns False if another instance is running."""
    LOCKFILE.parent.mkdir(exist_ok=True)
    if LOCKFILE.exists():
        try:
            existing_pid = int(LOCKFILE.read_text().strip())
            # Check if that PID is still alive
            import psutil
            if psutil.pid_exists(existing_pid):
                return False
        except Exception:
            pass  # Stale lock — overwrite it
    LOCKFILE.write_text(str(os.getpid()))
    return True


def release_lock():
    try:
        if LOCKFILE.exists():
            LOCKFILE.unlink()
    except Exception:
        pass


# ====================================================================== #
# IST Timezone helper
# ====================================================================== #

IST_OFFSET = timedelta(hours=5, minutes=30)


def now_ist() -> datetime:
    """Return current time in IST (timezone-naive for simplicity)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + IST_OFFSET


def now_ist_str() -> str:
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")


# ====================================================================== #
# P&L Calculator
# ====================================================================== #

FEE_RATE = 0.0005  # 0.05% per side (Delta maker/taker approximate)


def calc_pnl(direction: str, entry: float, exit_price: float, qty: float, fee_rate: float = FEE_RATE):
    """
    Calculate gross P&L, fees, and net P&L.

    LONG:  gross = (exit - entry) * qty
    SHORT: gross = (entry - exit) * qty

    Returns: (gross_pnl, fee, net_pnl)
    """
    if direction == "LONG":
        gross = (exit_price - entry) * qty
    else:  # SHORT
        gross = (entry - exit_price) * qty

    # Fee on both entry and exit notional
    fee = round((entry * qty * fee_rate) + (exit_price * qty * fee_rate), 4)
    net = round(gross - fee, 4)
    return round(gross, 4), fee, net


# ====================================================================== #
# Main Engine Class
# ====================================================================== #

class PaperEngine:
    """
    Offline paper trading engine.
    Processes one closed candle per analysis cycle.
    Manages simulated positions and balance.
    """

    def __init__(self):
        self.running = False
        self.paused = False
        self._stop_event = threading.Event()
        self._data_client: Optional[DeltaPublicClient] = None
        self._last_candle_ts = None
        self._df_cache = None
        self._ema_strategy = None
        self._ema_strategy_config_hash = None
        self._execution = PaperExecutionAdapter()
        self._delta_client = None
        self._delta_product = None
        self._delta_ready = False
        self._delta_account_clear = False
        self._delta_settlement_asset = None
        self._delta_settlement_asset_id = None
        self._delta_risk_error = None

    # ------------------------------------------------------------------ #
    # Startup
    # ------------------------------------------------------------------ #

    def start(self):
        """Initialize everything and start the main loop."""
        logger.info("=" * 70)
        logger.info("PAPER TRADING ENGINE — STARTING")
        logger.info("=" * 70)
        log_event("INFO", "engine", "Engine starting")

        # 1. Initialize database
        try:
            init_db()
            check_and_reset_daily()
            logger.info("✅ Database initialized")
            log_event("INFO", "engine", "Database initialized")
        except Exception as e:
            logger.error(f"Database init failed: {e}")
            sys.exit(1)

        # 2. Generate sample data if CSV doesn't exist
        generate_sample_csv()

        # 3. Initialize data client
        fallback_csv = config.get("data.fallback_csv", "data/btc_1m_sample.csv")
        delta_demo_mode = config.get("execution.mode", "paper") == "delta_demo"
        self._data_client = DeltaPublicClient(
            fallback_csv=fallback_csv, env="testnet" if delta_demo_mode else None)
        if delta_demo_mode:
            self._connect_delta_demo()

        # 4. Test data connection
        self._test_data_connection()

        # 5. Load persisted state
        state = get_state()
        logger.info(f"✅ State restored — Balance: ${state.get('balance', 1000.0):.2f} "
                    f"| Daily Trades: {state.get('daily_trades', 0)}")
        log_event("INFO", "engine", f"State restored. Balance=${state.get('balance', 1000.0):.2f}")

        # 6. Mark as running
        self.running = True
        update_state(engine_status="running", engine_heartbeat=now_ist_str())
        mode_label = "Delta Demo execution" if config.get("execution.mode", "paper") == "delta_demo" else "paper simulator active"
        log_event("INFO", "engine", f"Engine running — {mode_label}")

        try:
            from backtest_engine import BacktestEngine
            self._backtest_engine = BacktestEngine(self._data_client)
            self._backtest_engine.start()
        except Exception as exc:
            self._backtest_engine = None
            logger.exception("24-hour backtest service could not start: %s", exc)
            log_event("WARNING", "backtest", f"24-hour backtest service could not start: {exc}")

        # 7. Main loop
        self._main_loop()

    def _test_data_connection(self):
        """Test data source and report status."""
        symbol = config.get("general.symbol", "BTCUSDT")
        df = self._data_client.get_candles(symbol, config.get("data.timeframe", "1m"), limit=10)

        if df is not None and not df.empty:
            price = df["close"].iloc[-1]
            source = "CSV fallback" if self._data_client.is_using_fallback() else "Delta Exchange API"
            logger.info(f"✅ Data source: {source} | {symbol} @ ${price:,.2f}")
            log_event("INFO", "engine", f"Data connected via {source}. {symbol}=${price:,.2f}")
        else:
            err = self._data_client.get_last_error()
            logger.warning(f"⚠️ Data unavailable: {err}. Will retry.")
            log_event("WARNING", "engine", f"Data unavailable: {err}")

    def _connect_delta_demo(self):
        """Connect to testnet and refuse entries around unknown exposure."""
        try:
            if config.get("general.symbol", "BTCUSD") != "BTCUSD":
                raise DeltaAPIError("Delta Demo execution is restricted to BTCUSD")
            self._delta_client = DeltaDemoClient()
            self._delta_product = self._delta_client.get_product("BTCUSD")
            if self._delta_product.get("trading_status") != "operational":
                raise DeltaAPIError("BTCUSD demo contract is not operational")
            position = self._delta_client.get_position(self._delta_product["id"])
            orders = self._delta_client.get_open_orders(self._delta_product["id"])
            if int((position or {}).get("size") or 0) != 0 or orders:
                raise DeltaAPIError("Existing BTCUSD position/order found; clear it on the Demo account before starting")
            settlement = self._delta_product.get("settling_asset") or {}
            settlement_symbol = settlement.get("symbol")
            self._delta_settlement_asset_id = settlement.get("id")
            wallets = self._delta_client.get_wallet_balances()
            wallet = self._match_settlement_wallet(wallets, settlement_symbol, self._delta_settlement_asset_id)
            if wallet is None:
                available = ", ".join(str(w.get("asset_symbol") or w.get("asset_id")) for w in wallets) or "none returned"
                expected = settlement_symbol or self._delta_settlement_asset_id or "unknown"
                raise DeltaAPIError(f"BTCUSD settlement wallet not found (expected {expected}; received {available})")
            self._delta_settlement_asset = wallet.get("asset_symbol") or settlement_symbol
            if not self._delta_settlement_asset:
                raise DeltaAPIError("Matched settlement wallet has no asset symbol")
            current_balance = float(wallet.get("balance") or 0)
            day_key = now_ist().date().isoformat()
            old_status = get_exchange_status()
            baseline = (float(old_status.get("daily_baseline"))
                        if old_status.get("daily_baseline_date") == day_key and old_status.get("daily_baseline") is not None
                        else current_balance)
            self._delta_account_clear = True
            self._delta_risk_error = None
            self._delta_ready = bool(config.get("execution.demo_trading_enabled", False))
            update_exchange_status({"environment": "Delta India Demo/Testnet", "connected": True,
                                    "ready": self._delta_ready, "symbol": "BTCUSD", "position": position,
                                    "read_check_ok": True, "checked_at": now_ist_str(),
                                    "settlement_asset": self._delta_settlement_asset,
                                    "daily_baseline": baseline, "daily_baseline_date": day_key,
                                    "account_balance": current_balance,
                                    "open_orders": orders,
                                    "message": ("Ready; account exposure is clear" if self._delta_ready
                                                else "Connected read-only; automatic Demo orders remain disabled in Settings")})
            log_event("INFO", "delta_demo", "Demo API connected; order permission setting is " +
                      ("enabled" if self._delta_ready else "disabled"))
        except Exception as exc:
            self._delta_ready = False
            self._delta_account_clear = False
            self._delta_client = None
            safe_message = str(exc)[:240]
            logger.error(f"Delta Demo verification failed: {safe_message}")
            status = get_exchange_status()
            status.update(environment="Delta India Demo/Testnet", connected=False,
                          ready=False, symbol="BTCUSD", checked_at=now_ist_str(),
                          message=safe_message)
            update_exchange_status(status)
            log_event("ERROR", "delta_demo", f"Demo execution blocked: {safe_message}")

    @staticmethod
    def _match_settlement_wallet(wallets, symbol=None, asset_id=None):
        if symbol:
            match = next((w for w in wallets if str(w.get("asset_symbol", "")).upper() == str(symbol).upper()), None)
            if match:
                return match
        if asset_id is not None:
            return next((w for w in wallets if str(w.get("asset_id")) == str(asset_id)), None)
        return None

    def _get_delta_daily_pnl(self):
        """Realized wallet change plus current unrealized P&L in settlement units."""
        if not self._delta_client or not self._delta_product or not self._delta_settlement_asset:
            # Startup verification may have failed because of a temporary API
            # outage. Retry the read-only account check before rejecting a signal.
            self._connect_delta_demo()
        if not self._delta_client or not self._delta_product or not self._delta_settlement_asset:
            status = get_exchange_status()
            reason = status.get("message") or "Demo account connection is not initialized"
            raise DeltaAPIError(f"Delta account risk data unavailable: {reason}")
        wallets = self._delta_client.get_wallet_balances()
        wallet = self._match_settlement_wallet(wallets, self._delta_settlement_asset,
                                               self._delta_settlement_asset_id)
        if wallet is None:
            available = ", ".join(str(w.get("asset_symbol") or w.get("asset_id")) for w in wallets) or "none returned"
            raise DeltaAPIError(f"Settlement wallet {self._delta_settlement_asset} unavailable; received {available}")
        current_balance = float(wallet.get("balance") or 0)
        day_key = now_ist().date().isoformat()
        status = get_exchange_status()
        baseline = status.get("daily_baseline")
        if status.get("daily_baseline_date") != day_key or baseline is None:
            baseline = current_balance
        position = self._delta_client.get_position(self._delta_product["id"])
        unrealized = float((position or {}).get("unrealized_pnl") or 0)
        daily_pnl = current_balance - float(baseline) + unrealized
        self._delta_risk_error = None
        status.update(settlement_asset=self._delta_settlement_asset,
                      daily_baseline=float(baseline), daily_baseline_date=day_key,
                      account_balance=current_balance, daily_pnl=daily_pnl,
                      connected=True, ready=bool(self._delta_ready),
                      message="Delta Demo wallet and daily risk checks verified")
        update_exchange_status(status)
        return daily_pnl, float(baseline)

    # ------------------------------------------------------------------ #
    # Main Loop
    # ------------------------------------------------------------------ #

    def _main_loop(self):
        """
        Main analysis loop.
        Runs every `analysis_interval_sec` seconds (default 60).
        Processes exactly ONE new closed candle per cycle.
        """
        analysis_interval = config.get("processing.analysis_interval_sec", 60)
        pos_check_interval = config.get("processing.position_check_interval_sec", 30)
        last_analysis = 0.0
        last_pos_check = 0.0
        last_heartbeat = 0.0
        last_daily_check = 0.0
        self._ack_config()

        logger.info(f"🔄 Main loop started — analysis every {analysis_interval}s, "
                    f"position check every {pos_check_interval}s")

        while not self._stop_event.is_set():
            loop_start = time.monotonic()

            try:
                now = time.monotonic()
                if now - last_heartbeat >= 10:
                    update_state(engine_heartbeat=now_ist_str())
                    last_heartbeat = now
                if now - last_daily_check >= 30:
                    check_and_reset_daily()
                    last_daily_check = now

                # Poll config independently from analysis cadence; apply without restart.
                config.reload()
                self._ack_config()
                if (config.get("execution.mode", "paper") == "delta_demo" and
                        self._delta_account_clear and self._delta_client is not None):
                    requested_enabled = bool(config.get("execution.demo_trading_enabled", False))
                    if requested_enabled != self._delta_ready:
                        self._delta_ready = requested_enabled
                        exchange_status = get_exchange_status()
                        exchange_status.update(ready=requested_enabled,
                            message=("Ready; account exposure is clear" if requested_enabled
                                     else "Connected read-only; automatic Demo orders are disabled in Settings"))
                        update_exchange_status(exchange_status)
                analysis_interval = config.get("processing.analysis_interval_sec", 60)
                pos_check_interval = config.get("processing.position_check_interval_sec", 30)

                if not self.paused:
                    if now - last_analysis >= analysis_interval:
                        self._run_analysis()
                        last_analysis = time.monotonic()
                    if time.monotonic() - last_pos_check >= pos_check_interval:
                        self._manage_positions()
                        last_pos_check = time.monotonic()

            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
                log_event("ERROR", "engine", f"Main loop error: {e}")
                time.sleep(5)
                continue

            # One-second poll keeps config changes responsive while analysis cadence stays configurable.
            elapsed = time.monotonic() - loop_start
            self._stop_event.wait(timeout=max(0.1, 1.0 - elapsed))

    @staticmethod
    def _ack_config():
        """Persist an acknowledgement after the engine reads the current config."""
        path = Path(__file__).parent / "data" / "config_status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        applied_hash = config.fingerprint()
        previous_hash = None
        try:
            previous_hash = json.loads(path.read_text(encoding="utf-8")).get("applied_hash")
        except (OSError, ValueError):
            pass
        if previous_hash == applied_hash:
            return
        temp.write_text(json.dumps({"applied_hash": applied_hash, "applied_at": now_ist_str()}), encoding="utf-8")
        temp.replace(path)
        if previous_hash and previous_hash != applied_hash:
            log_event("INFO", "engine", "Updated configuration applied")

    # ------------------------------------------------------------------ #
    # Analysis cycle
    # ------------------------------------------------------------------ #

    def _run_analysis(self):
        """Fetch data, check for new candle, run strategy."""
        symbol = config.get("general.symbol", "BTCUSDT")
        resolution = config.get("data.timeframe", "1m")
        limit = config.get("data.candle_history", 200)
        df = self._data_client.get_candles(symbol, resolution, limit)
        if df is None or df.empty:
            err = self._data_client.get_last_error()
            logger.warning(f"No data received: {err}")
            update_strategy_runtime(config.get("general.strategy_name", ""), None)
            update_state(engine_status="data_unavailable")
            return

        forming_candle = bool(getattr(self._data_client, "last_candle_forming", True))
        if len(df) < 3:
            log_event("WARNING", "engine", "Not enough candles to evaluate a signal")
            return
        closed = df.iloc[-2] if forming_candle else df.iloc[-1]
        candle_ts = str(closed["time"])
        if candle_ts == self._last_candle_ts:
            return
        self._last_candle_ts = candle_ts

        # Strategy selection is executable configuration, not a display label.
        strategy_name = config.get("general.strategy_name", "SMC BB Breakout")
        cfg_ind = config.get_section("indicators")
        signal_id = None
        signal_stop = None
        if strategy_name in EMA_STRATEGY_NAMES:
            ema_cfg = {key: cfg_ind.get(key) for key in (
                "ema_fast_period", "ema_slow_period", "ema_crossover_enabled",
                "bullish_engulfing_enabled", "bearish_engulfing_enabled",
                "engulfing_wait_candles", "ema_signal_mode")}
            ema_cfg["trade_direction"] = config.get("entry.trade_direction", "both")
            ema_cfg["allow_long"] = config.get("entry.allow_long", True)
            ema_cfg["allow_short"] = config.get("entry.allow_short", True)
            cfg_hash = json.dumps(ema_cfg, sort_keys=True)
            if cfg_hash != self._ema_strategy_config_hash:
                if self._ema_strategy is None:
                    self._ema_strategy = EMAEngulfingStrategy(ema_cfg)
                else:
                    self._ema_strategy.reset(ema_cfg)
                    log_event("INFO", "engine", "EMA strategy settings changed; pending setup cleared")
                self._ema_strategy_config_hash = cfg_hash
            pending_before = self._ema_strategy.pending_snapshot()
            signal_obj = self._ema_strategy.process(df, resolution, forming_candle)
            pending_after = self._ema_strategy.pending_snapshot()
            if pending_before and (not pending_after or pending_before["crossover_time"] != pending_after["crossover_time"]):
                if signal_obj and signal_obj.crossover_time == pending_before["crossover_time"]:
                    log_event("INFO", "strategy", f"{pending_before['direction']} Engulfing confirmation received")
                else:
                    log_event("INFO", "strategy", f"Pending {pending_before['direction']} setup cleared (expiry or opposite crossover)")
            if pending_after and (not pending_before or pending_before["crossover_time"] != pending_after["crossover_time"]):
                log_event("INFO", "strategy", f"EMA 9/15 {pending_after['direction']} crossover detected at {pending_after['crossover_time']}")
                log_event("INFO", "strategy", f"Waiting for {pending_after['direction']} Engulfing confirmation")
            elif pending_after and pending_before and pending_after["candles_waited"] != pending_before["candles_waited"]:
                log_event("INFO", "strategy", f"Still waiting for Engulfing confirmation ({pending_after['candles_waited']}/{cfg_ind.get('engulfing_wait_candles', 3)} candles)")
            if signal_obj:
                log_event("INFO", "strategy", f"{signal_obj.reason} confirmed; {signal_obj.direction} signal generated")
            update_strategy_runtime(strategy_name, pending_after)
            if signal_obj:
                signal, reason = signal_obj.direction, signal_obj.reason
                signal_id, signal_stop = signal_obj.signal_id, signal_obj.stop_loss
            else:
                signal, reason = None, ""
        else:
            self._ema_strategy = None
            self._ema_strategy_config_hash = None
            df = apply_all_indicators(df.copy(), cfg_ind)
            signal, reason = detect_signals(df, cfg_ind, config.get_section("entry"))
            update_strategy_runtime(strategy_name, None)

        update_state(last_candle_ts=candle_ts, engine_status="running")
        price = self._data_client.get_current_price(symbol)
        if signal and (price is None or float(price) <= 0 or not self._data_client.is_price_fresh(symbol, max_age_sec=10)):
            log_event("ERROR", "engine", "Signal rejected: fresh executable ticker price is unavailable")
            log_signal(symbol, signal, float(closed["close"]), "Rejected: fresh market ticker unavailable", acted_upon=False)
            return
        price = float(price) if price is not None else float(closed["close"])
        logger.info(f"📊 New closed candle: {candle_ts} | {symbol} @ ${price:,.2f}")

        # Check if we can open new positions
        state = get_state()
        open_pos = get_open_positions()
        if config.get("execution.mode", "paper") == "delta_demo":
            open_pos = [p for p in open_pos if p.get("entry_config", {}).get("delta_entry_order_id")]
        daily_trades = state.get("daily_trades", 0)
        daily_pnl = state.get("daily_pnl", 0.0)
        balance = state.get("balance", 1000.0)
        delta_risk_capital = None
        if signal and config.get("execution.mode", "paper") == "delta_demo":
            try:
                daily_pnl, delta_risk_capital = self._get_delta_daily_pnl()
            except Exception as exc:
                message = f"Exchange daily risk balance unavailable: {str(exc)[:180]}"
                # Block this entry but retain a verified client so exchange
                # position and protective-order monitoring can continue.
                self._delta_risk_error = message
                status = get_exchange_status()
                status.update(environment="Delta India Demo/Testnet",
                              connected=bool(self._delta_client and self._delta_product),
                              ready=False, symbol="BTCUSD", message=message)
                update_exchange_status(status)
                log_event("ERROR", "delta_demo", message)
                self._reject_signal(symbol, signal, price, "Exchange daily risk check unavailable")
                return

        max_daily_trades = config.get("entry.max_daily_trades", 5)
        max_daily_loss_pct = config.get("risk.max_daily_loss_pct", 5.0)
        max_positions = config.get("risk.max_open_positions", 1)

        # Daily loss limit check
        capital = config.get("risk.capital", 1000.0)
        risk_basis = delta_risk_capital if delta_risk_capital is not None else capital
        daily_loss_limit = (config.get("risk.max_daily_loss_amount", 0) or
                            risk_basis * (max_daily_loss_pct / 100.0))

        if daily_pnl <= -daily_loss_limit:
            unit = self._delta_settlement_asset or "$" if delta_risk_capital is not None else "$"
            self._reject_signal(symbol, signal, price, f"Daily loss limit reached ({daily_pnl:.2f} {unit})")
            return

        if daily_trades >= max_daily_trades:
            self._reject_signal(symbol, signal, price, f"Maximum daily trades reached ({daily_trades}/{max_daily_trades})")
            return

        profit_target_pct = config.get("risk.max_daily_profit_pct", 0.0)
        if profit_target_pct > 0 and daily_pnl >= risk_basis * profit_target_pct / 100.0:
            self._reject_signal(symbol, signal, price, "Daily profit target reached")
            return
        loss_limit = int(config.get("risk.max_consecutive_losses", 0))
        if loss_limit > 0 and config.get("execution.mode", "paper") == "delta_demo":
            self._reject_signal(symbol, signal, price, "Consecutive-loss rule needs exchange fill-history sync; demo entry blocked")
            return
        if loss_limit > 0:
            streak = 0
            for trade in get_all_trades(limit=loss_limit):
                if float(trade.get("net_pnl") or 0) < 0:
                    streak += 1
                else:
                    break
            if streak >= loss_limit:
                self._reject_signal(symbol, signal, price, "Maximum consecutive losses reached")
                return
        risk_cfg = config.get_section("risk")
        if risk_cfg.get("trading_session") == "custom":
            current = now_ist().strftime("%H:%M")
            start, end = risk_cfg.get("trading_start_time", "00:00"), risk_cfg.get("trading_end_time", "23:59")
            in_session = (start <= current <= end) if start <= end else (current >= start or current <= end)
            if not in_session:
                self._reject_signal(symbol, signal, price, "Outside configured trading session")
                return

        if len(open_pos) >= max_positions:
            self._reject_signal(symbol, signal, price, f"Maximum open positions reached ({len(open_pos)}/{max_positions})")
            return

        if signal:
            if has_signal_been_traded(signal_id):
                self._reject_signal(symbol, signal, price, "Duplicate signal already traded")
                return
            logger.info(f"🚦 Signal: {signal} | {reason}")
            log_signal(symbol, signal, price, reason, acted_upon=False)
            self._open_position(signal, price, reason, balance, now_ist_str(), signal_stop, signal_id)
        else:
            logger.debug(f"No confirmed signal | {symbol} @ ${price:,.2f}")

    @staticmethod
    def _reject_signal(symbol, signal, price, reason):
        if not signal:
            return
        logger.warning(f"{signal} signal rejected: {reason}")
        log_event("WARNING", "engine", f"{signal} signal rejected: {reason}")
        log_signal(symbol, signal, float(price), f"Rejected: {reason}", acted_upon=False)

    # ------------------------------------------------------------------ #
    # Open position
    # ------------------------------------------------------------------ #

    def _open_position(self, signal: str, price: float, reason: str,
                       balance: float, entry_time: str, signal_stop=None, signal_id=None):
        """Create a new simulated position."""
        cfg_exit = config.get_section("exit")
        cfg_risk = config.get_section("risk")
        symbol = config.get("general.symbol", "BTCUSDT")
        execution_cfg = config.get_section("execution")
        if config.get("execution.mode", "paper") == "delta_demo":
            exchange_balance = get_exchange_status().get("account_balance")
            if exchange_balance is None:
                self._reject_signal(symbol, signal, price, "Demo wallet balance is unavailable for position sizing")
                return
            balance = float(exchange_balance)
        if not cfg_exit.get("mandatory_stop_loss", True):
            self._reject_signal(symbol, signal, price,
                                "Mandatory Stop Loss is disabled; new entries are locked for safety")
            return
        slippage = float(execution_cfg.get("slippage_bps", 2.0)) / 10000.0
        fill_price = price * (1.0 + slippage if signal == "LONG" else 1.0 - slippage)
        try:
            if signal_stop is not None:
                ema_mode = config.get("indicators.ema_sl_mode", "candle_extreme")
                if ema_mode in {"pct", "fixed"}:
                    sl_price = calculate_initial_stop(signal, fill_price, "custom", signal_stop=signal_stop,
                        custom_mode=ema_mode, custom_value=cfg_exit.get("sl_value", 50))
                else:
                    sl_price = calculate_initial_stop(signal, fill_price, ema_mode,
                        points=cfg_exit.get("sl_points", 50), signal_stop=signal_stop)
            else:
                sl_price = calculate_initial_stop(signal, fill_price,
                    cfg_exit.get("sl_calculation_mode", "fixed_points"),
                    points=cfg_exit.get("sl_points", 50),
                    custom_mode=cfg_exit.get("sl_mode"), custom_value=cfg_exit.get("sl_value"))
        except (TypeError, ValueError) as exc:
            self._reject_signal(symbol, signal, price, f"Initial protective stop is invalid: {exc}")
            return
        risk_distance = abs(fill_price - sl_price)
        sl_error = validate_stop(signal, fill_price, sl_price, cfg_exit.get("minimum_sl_points", 0.01))
        if sl_error:
            self._reject_signal(symbol, signal, price, f"Calculated stop loss is invalid: {sl_error}")
            return
        confirmation = self._execution.confirm_entry_stop(
            signal, fill_price, sl_price, cfg_exit.get("minimum_sl_points", 0.01))
        if not confirmation.get("accepted"):
            self._reject_signal(symbol, signal, price,
                                f"Protective stop confirmation failed: {confirmation.get('error', 'unknown error')}")
            return
        sl_price = float(confirmation["stop_price"])
        use_tp = bool(cfg_exit.get("use_take_profit", False))
        if use_tp:
            if cfg_exit.get("tp_mode") == "risk_reward":
                rr_target = float(cfg_exit.get("risk_reward_ratio", config.get("risk.risk_reward_ratio", 3.0)))
                tp_price = fill_price + risk_distance * rr_target if signal == "LONG" else fill_price - risk_distance * rr_target
                tp_price = round(tp_price, 2)
            else:
                _, tp_price = calc_sl_tp(signal, fill_price, cfg_exit)
            if (signal == "LONG" and tp_price <= fill_price) or (signal == "SHORT" and tp_price >= fill_price):
                self._reject_signal(symbol, signal, price, "Calculated take profit is on the wrong side of entry")
                return
        else:
            tp_price = 0.0
        cfg_risk["capital"] = max(float(balance), 0.0)
        fee_rate = float(execution_cfg.get("fee_rate_per_side", FEE_RATE))
        quantity = (float(cfg_risk.get("fixed_lot_size", 0.01))
                    if cfg_risk.get("position_size_mode") == "fixed_lot"
                    else calc_quantity(fill_price, sl_price, cfg_risk, fee_rate, slippage))
        if quantity <= 0:
            self._reject_signal(symbol, signal, price, "Calculated position size is invalid")
            return
        if cfg_risk.get("position_size_mode") != "fixed_lot":
            risk_budget = max(float(balance), 0.0) * float(cfg_risk.get("risk_pct", 0.0)) / 100.0
            estimated_risk = quantity * (risk_distance + (fill_price + sl_price) * (fee_rate + slippage))
            if estimated_risk > risk_budget * 1.001:
                self._reject_signal(symbol, signal, price, "Minimum lot would exceed configured risk budget")
                return

        if config.get("execution.mode", "paper") == "delta_demo":
            if not self._delta_ready or self._delta_client is None:
                self._reject_signal(symbol, signal, price, "Delta Demo is not ready; check Monitor for connection or account-state details")
                return
            try:
                fill_price, quantity, sl_price, tp_price, delta_entry_order_id = self._execute_delta_demo(
                    signal, signal_id, quantity, fill_price, sl_price, tp_price if use_tp else None,
                    risk_distance, cfg_exit.get("use_trailing_stop", True))
                risk_distance = abs(fill_price - sl_price)
            except Exception as exc:
                self._delta_ready = False
                self._delta_account_clear = False
                message = str(exc)[:240]
                update_exchange_status({"environment": "Delta India Demo/Testnet", "connected": True,
                                        "ready": False, "symbol": symbol,
                                        "message": f"Trade blocked; verify exchange state manually: {message}"})
                log_event("ERROR", "delta_demo", f"Order flow halted: {message}")
                self._reject_signal(symbol, signal, price, f"Delta Demo order/protection not confirmed: {message}")
                return

        trailing_settings = {key: cfg_exit.get(key) for key in (
            "trailing_method", "trailing_update_frequency", "breakeven_trigger_rr",
            "profit_lock_trigger_rr", "profit_lock_level_rr", "trailing_stop_points",
            "trailing_stop_pct", "atr_period", "atr_multiplier", "fee_adjust_breakeven")
            if cfg_exit.get(key) is not None}
        entry_config = {
            "fee_rate_per_side": fee_rate,
            "slippage_bps": float(execution_cfg.get("slippage_bps", 2.0)),
            "timeframe": config.get("data.timeframe", "1m"),
            "use_take_profit": use_tp,
            "execution_venue": "DELTA_TESTNET" if config.get("execution.mode", "paper") == "delta_demo" else "PAPER_ONLY",
        }
        if config.get("execution.mode", "paper") == "delta_demo":
            entry_config["delta_entry_order_id"] = delta_entry_order_id
        try:
            pos_id = save_position(
                symbol=symbol, direction=signal, entry_price=fill_price, quantity=quantity,
                sl_price=sl_price, tp_price=tp_price, entry_time=entry_time,
                signal_reason=reason, sl_enabled=True, tp_enabled=use_tp,
                signal_id=signal_id, trailing_enabled=cfg_exit.get("use_trailing_stop", True),
                trailing_stop_pct=cfg_exit.get("trailing_stop_pct", 0.3),
                initial_sl_price=sl_price, initial_risk_points=risk_distance,
                trailing_settings=trailing_settings, entry_config=entry_config,
                rr_milestones_enabled=(cfg_exit.get("trailing_method") != "fixed_points"))
        except Exception as exc:
            if config.get("execution.mode", "paper") == "delta_demo":
                self._delta_ready = False
                self._delta_account_clear = False
            self._reject_signal(symbol, signal, price, f"Could not persist protective stop: {exc}")
            return

        sl_pct = abs(fill_price - sl_price) / fill_price * 100
        tp_pct = abs(tp_price - fill_price) / fill_price * 100 if use_tp else 0.0
        tp_log = f"${tp_price:,.2f} ({tp_pct:.2f}%)" if use_tp else "Disabled"
        risk_amt = round(quantity * (risk_distance + (fill_price + sl_price) * (fee_rate + slippage)), 2)

        logger.info(
            f"\n{'='*65}\n"
            f"{'🟢 LONG' if signal == 'LONG' else '🔴 SHORT'} POSITION OPENED  [ID:{pos_id}]\n"
            f"  Symbol:   {symbol}\n"
            f"  Entry:    ${fill_price:,.2f}\n"
            f"  Qty:      {quantity} BTC\n"
            f"  SL:       ${sl_price:,.2f} ({sl_pct:.2f}%)\n"
            f"  TP:       {tp_log}\n"
            f"  Risk:     ${risk_amt:.2f}\n"
            f"  Signal:   {reason}\n"
            f"{'='*65}"
        )
        protection_source = "exchange" if config.get("execution.mode", "paper") == "delta_demo" else "simulated"
        log_event("INFO", "engine",
                  f"{signal} opened @ ${fill_price:,.2f} | qty={quantity} BTC | {protection_source} protective SL confirmed=${sl_price:.2f} | TP={'$%.2f' % tp_price if use_tp else 'disabled'} | fee={execution_cfg.get('fee_rate_per_side', FEE_RATE):.4%}/side | slippage={execution_cfg.get('slippage_bps', 2.0)}bps | {reason}")
        log_signal(symbol, signal, fill_price, reason, acted_upon=True)
        if config.get("execution.mode", "paper") == "delta_demo":
            latest_state = get_state()
            update_state(daily_trades=int(latest_state.get("daily_trades", 0)) + 1)

    def _execute_delta_demo(self, direction, signal_id, quantity_btc, indicative_fill,
                            sl_price, tp_price, risk_distance, trailing_enabled):
        """Submit one testnet market entry and require exchange-side protection."""
        product = self._delta_product
        contract_value = float(product.get("contract_value") or 0)
        if contract_value != 0.001:
            raise DeltaAPIError("BTCUSD contract value changed; refusing stale BTC-to-contract conversion")
        contracts = int(round(float(quantity_btc) / contract_value))
        if contracts <= 0 or abs(contracts * contract_value - float(quantity_btc)) > 1e-8:
            raise DeltaAPIError("Configured BTC quantity cannot be represented as whole BTCUSD contracts")

        # Client order IDs make retries/restarts identifiable. Never retry an
        # ambiguous order submission automatically.
        import uuid
        client_id = uuid.uuid5(uuid.NAMESPACE_URL, f"tradingbot:{signal_id or time.time_ns()}").hex
        order_result = self._delta_client.place_market_order(product["id"], "BTCUSD", direction, contracts, client_id)
        order_id = (order_result or {}).get("id")

        position = None
        for _ in range(12):
            position = self._delta_client.get_position(product["id"])
            signed_size = int((position or {}).get("size") or 0)
            if signed_size:
                break
            time.sleep(0.5)
        signed_size = int((position or {}).get("size") or 0)
        expected_sign = 1 if direction == "LONG" else -1
        if not signed_size or (1 if signed_size > 0 else -1) != expected_sign:
            raise DeltaAPIError("Market entry was not confirmed as an exchange position")
        actual_contracts = abs(signed_size)
        if actual_contracts > contracts:
            raise DeltaAPIError("Exchange position exceeds submitted quantity; manual review required")
        if trailing_enabled and not order_id:
            self._delta_client.close_position_market(product["id"], actual_contracts,
                direction, uuid.uuid5(uuid.NAMESPACE_URL, f"{client_id}:reduce").hex)
            raise DeltaAPIError("Exchange did not return an entry order ID; reduce-only close submitted because trailing updates cannot be linked")

        entry = float(position.get("entry_price") or indicative_fill)
        distance = abs(float(risk_distance))
        actual_sl = entry - distance if direction == "LONG" else entry + distance
        actual_tp = None
        if tp_price is not None:
            tp_distance = abs(float(tp_price) - float(indicative_fill))
            actual_tp = entry + tp_distance if direction == "LONG" else entry - tp_distance
        tick = float(product.get("tick_size") or 0.5)
        if tick <= 0:
            raise DeltaAPIError("Invalid BTCUSD tick size")

        def tick_price(value, upward):
            scaled = float(value) / tick
            rounded = math.ceil(scaled - 1e-10) if upward else math.floor(scaled + 1e-10)
            return round(rounded * tick, 8)

        actual_sl = tick_price(actual_sl, direction == "SHORT")
        if actual_tp:
            actual_tp = tick_price(actual_tp, direction == "LONG")
        try:
            self._delta_client.place_bracket(product["id"], "BTCUSD", actual_sl,
                                             actual_tp if actual_tp else None)
        except Exception:
            # Protect against an unprotected demo position if bracket creation fails.
            self._delta_client.close_position_market(product["id"], actual_contracts,
                direction, uuid.uuid5(uuid.NAMESPACE_URL, f"{client_id}:reduce").hex)
            raise DeltaAPIError("Protective bracket rejected; reduce-only close submitted and trading halted")
        update_exchange_status({"environment": "Delta India Demo/Testnet", "connected": True,
                                "ready": True, "symbol": "BTCUSD", "position": position,
                                "open_orders": [], "protective_sl": actual_sl,
                                "protective_tp": actual_tp if actual_tp else None,
                                "message": "Demo position open; exchange bracket confirmed"})
        return entry, actual_contracts * contract_value, actual_sl, actual_tp if actual_tp else 0.0, order_id

    # ------------------------------------------------------------------ #
    # Position management
    # ------------------------------------------------------------------ #

    def _manage_positions(self):
        """Check all open positions for SL/TP hits."""
        if config.get("execution.mode", "paper") == "delta_demo":
            if self._delta_client and self._delta_product:
                try:
                    position = self._delta_client.get_position(self._delta_product["id"])
                    orders = self._delta_client.get_open_orders(self._delta_product["id"])
                    size = int((position or {}).get("size") or 0)
                    local_snapshot = [p for p in get_open_positions()
                                      if p.get("entry_config", {}).get("delta_entry_order_id")]
                    local_position = local_snapshot[0] if local_snapshot else {}
                    old_status = get_exchange_status()
                    if self._delta_risk_error:
                        sync_message = self._delta_risk_error
                    elif self._delta_ready:
                        sync_message = "Position and open orders verified from Delta Demo"
                    elif self._delta_account_clear and not config.get("execution.demo_trading_enabled", False):
                        sync_message = "Connection and account verified; automatic orders disabled in Settings"
                    else:
                        sync_message = old_status.get("message") or "Trading stopped; review the displayed connection reason"
                    update_exchange_status({"environment": "Delta India Demo/Testnet", "connected": True,
                                            "ready": bool(self._delta_ready and not self._delta_risk_error), "symbol": "BTCUSD",
                                            "read_check_ok": True, "checked_at": now_ist_str(),
                                            "position": position, "open_orders": orders,
                                            "protective_sl": local_position.get("sl_price"),
                                            "protective_tp": local_position.get("tp_price") if local_position.get("entry_config", {}).get("use_take_profit") else None,
                                            "message": sync_message})
                    if size == 0 and local_snapshot:
                        for stale in local_snapshot:
                            close_position(stale["id"])
                        log_event("INFO", "delta_demo", "Exchange position is flat; removed local open-position shadow")
                    elif size and self._delta_ready:
                        local_positions = local_snapshot
                        if local_positions:
                            tracked = local_positions[0]
                            expected = int(round(float(tracked["quantity"]) / float(self._delta_product.get("contract_value") or 0.001)))
                            expected = expected if tracked["direction"] == "LONG" else -expected
                            if expected != size:
                                self._delta_ready = False
                                update_exchange_status({"environment": "Delta India Demo/Testnet", "connected": True,
                                    "ready": False, "symbol": "BTCUSD", "position": position,
                                    "open_orders": orders, "message": "Exchange size changed outside bot; trailing updates and new entries halted"})
                                log_event("ERROR", "delta_demo", "Exchange position differs from bot record; manual review required")
                                return
                        market = self._data_client.get_current_price("BTCUSD")
                        if local_positions and market and self._data_client.is_price_fresh("BTCUSD", max_age_sec=10):
                            pos = local_positions[0]
                            atr_value = None
                            timeframe = pos.get("entry_config", {}).get("timeframe", "1m")
                            needs_candle = (pos.get("trailing_method") == "atr" or
                                            pos.get("trailing_update_frequency") == "candle_close")
                            candle_close = True
                            candle_time = None
                            if needs_candle:
                                candles = self._data_client.get_candles("BTCUSD", timeframe,
                                    limit=max(int(pos.get("atr_period", 14)) + 2, 20))
                                if candles is None or candles.empty:
                                    candle_close = False
                                else:
                                    closed = candles.iloc[:-1] if getattr(self._data_client, "last_candle_forming", False) else candles
                                    if closed.empty:
                                        candle_close = False
                                    else:
                                        candle_time = str(closed.iloc[-1]["time"])
                                        candle_close = candle_time != pos.get("trailing_last_candle_time")
                                        if pos.get("trailing_method") == "atr":
                                            period = int(pos.get("atr_period", 14))
                                            previous = closed["close"].shift(1)
                                            tr = pd.concat([closed["high"] - closed["low"],
                                                (closed["high"] - previous).abs(),
                                                (closed["low"] - previous).abs()], axis=1).max(axis=1)
                                            if len(tr.dropna()) >= period:
                                                atr_value = float(tr.tail(period).mean())
                            if candle_close:
                                proposal = propose_trailing_stop(pos, float(market), atr_value)
                            else:
                                proposal = None
                            if proposal:
                                candidate = float(proposal["stop_price"])
                                current_stop = float(pos["sl_price"])
                                direction = pos["direction"]
                                tighter = candidate > current_stop if direction == "LONG" else candidate < current_stop
                                if tighter and not proposal.get("triggered"):
                                    order_id = pos.get("entry_config", {}).get("delta_entry_order_id")
                                    if not order_id:
                                        self._delta_ready = False
                                        log_event("ERROR", "delta_demo", "Cannot update trailing bracket: exchange entry order ID is unavailable")
                                    else:
                                        cfg = pos.get("entry_config", {})
                                        tp = pos.get("tp_price") if cfg.get("use_take_profit") else None
                                        tick = float(self._delta_product.get("tick_size") or 0.5)
                                        scaled = candidate / tick
                                        candidate = (math.floor(scaled + 1e-10) if direction == "LONG"
                                                     else math.ceil(scaled - 1e-10)) * tick
                                        candidate = round(candidate, 8)
                                        self._delta_client.update_bracket(order_id, self._delta_product["id"],
                                                                         candidate, tp)
                                        persisted = update_position_stop(pos["id"], candidate,
                                            proposal["best_price"], int(proposal["rr"]), proposal["status"])
                                        if not persisted:
                                            self._delta_ready = False
                                            log_event("ERROR", "delta_demo", "Exchange trailing stop updated but local confirmation failed; manual review required")
                                        else:
                                            update_exchange_status({"environment": "Delta India Demo/Testnet", "connected": True,
                                                "ready": True, "symbol": "BTCUSD", "position": position,
                                                "open_orders": orders, "protective_sl": candidate,
                                                "protective_tp": tp, "message": "Exchange trailing stop updated"})
                            if pos.get("trailing_update_frequency") == "candle_close" and candle_time:
                                update_trailing_candle_time(pos["id"], candle_time)
                except Exception as exc:
                    self._delta_ready = False
                    self._delta_account_clear = False
                    update_exchange_status({"environment": "Delta India Demo/Testnet", "connected": False,
                                            "ready": False, "symbol": "BTCUSD",
                                            "message": f"Position sync failed; entries halted: {str(exc)[:200]}"})
            return
        open_pos = get_open_positions()
        if not open_pos:
            return

        candle_cache = {}
        for pos in open_pos:
            price = self._data_client.get_current_price(pos["symbol"])
            if price is None or not self._data_client.is_price_fresh(pos["symbol"], max_age_sec=10):
                logger.warning(f"Cannot manage position {pos['id']} — price unavailable for {pos['symbol']}")
                continue
            atr_value = None
            candle_close = False
            candle_time = None
            needs_candles = (pos.get("trailing_enabled") and
                             (pos.get("trailing_method") == "atr" or
                              pos.get("trailing_update_frequency") == "candle_close"))
            if needs_candles:
                entry_cfg = pos.get("entry_config", {})
                timeframe = entry_cfg.get("timeframe", config.get("data.timeframe", "1m"))
                cache_key = (pos["symbol"], timeframe)
                if cache_key not in candle_cache:
                    candle_cache[cache_key] = self._data_client.get_candles(
                        pos["symbol"], timeframe, limit=max(int(pos.get("atr_period", 14)) + 2, 20))
                df = candle_cache[cache_key]
                if df is not None and not df.empty:
                    closed = df.iloc[:-1] if getattr(self._data_client, "last_candle_forming", False) else df
                    if not closed.empty:
                        candle_time = str(closed.iloc[-1]["time"])
                        candle_close = candle_time != pos.get("trailing_last_candle_time")
                        if pos.get("trailing_method") == "atr":
                            period = int(pos.get("atr_period", 14))
                            previous = closed["close"].shift(1)
                            true_range = pd.concat([
                                closed["high"] - closed["low"],
                                (closed["high"] - previous).abs(),
                                (closed["low"] - previous).abs(),
                            ], axis=1).max(axis=1)
                            if len(true_range.dropna()) >= period:
                                atr_value = float(true_range.tail(period).mean())
            self._check_exit(pos, price, atr_value=atr_value,
                             candle_close=candle_close, candle_time=candle_time)

    def _check_exit(self, pos: dict, current_price: float, atr_value=None,
                    candle_close=False, candle_time=None):
        """Apply a persisted, monotonic simulated stop and evaluate exits."""
        direction = pos["direction"]
        entry = pos["entry_price"]
        sl = pos["sl_price"]
        tp = pos["tp_price"]
        qty = pos["quantity"]
        pos_id = pos["id"]
        entry_time = pos["entry_time"]

        exit_reason = None

        if direction == "LONG":
            if pos.get("sl_enabled", 1) and current_price <= sl:
                exit_reason = "SL Hit"
            elif pos.get("tp_enabled", 1) and current_price >= tp:
                exit_reason = "TP Hit"
        else:  # SHORT
            if pos.get("sl_enabled", 1) and current_price >= sl:
                exit_reason = "SL Hit"
            elif pos.get("tp_enabled", 1) and current_price <= tp:
                exit_reason = "TP Hit"

        if not exit_reason and pos.get("trailing_enabled") and pos.get("sl_enabled", 1):
            frequency = pos.get("trailing_update_frequency", "tick")
            if frequency != "candle_close" or candle_close:
                proposal = propose_trailing_stop(pos, current_price, atr_value)
                if proposal:
                    candidate = float(proposal["stop_price"])
                    is_tighter = candidate > sl if direction == "LONG" else candidate < sl
                    if proposal.get("triggered"):
                        exit_reason = "Trailing SL Hit"
                    elif not (is_tighter or proposal["best_price"] != float(pos.get("best_favorable_price") or entry)
                              or proposal["status"] != pos.get("trailing_status", "active")):
                        pass  # identical signal state: avoid duplicate persistence work
                    else:
                        confirmation = self._execution.confirm_stop_update(pos, candidate)
                        if confirmation.get("accepted"):
                            persisted = update_position_stop(
                                pos_id, candidate, proposal["best_price"],
                                int(proposal["rr"]), proposal["status"])
                            if persisted:
                                pos["best_favorable_price"] = proposal["best_price"]
                                pos["trailing_milestone"] = int(proposal["rr"])
                                pos["trailing_status"] = proposal["status"]
                                if is_tighter:
                                    sl = float(confirmation["stop_price"])
                                    pos["sl_price"] = sl
                                    log_event("INFO", "trailing",
                                        f"Trade #{pos_id}: simulated stop confirmed at {sl:.2f} ({proposal['rr']:.2f}R; {proposal['status']})")
                            elif is_tighter:
                                log_event("ERROR", "trailing", f"Trade #{pos_id}: stop update was not persisted; keeping confirmed SL {sl:.2f}")
                        else:
                            log_event("ERROR", "trailing", f"Trade #{pos_id}: stop update rejected; keeping confirmed SL {sl:.2f}: {confirmation.get('error')}")
            if frequency == "candle_close" and candle_time is not None:
                update_trailing_candle_time(pos_id, candle_time)

        if exit_reason:
            self._close_position(pos, current_price, exit_reason)

        else:
            # Log unrealized P&L
            if direction == "LONG":
                unreal = round((current_price - entry) * qty, 2)
            else:
                unreal = round((entry - current_price) * qty, 2)
            logger.debug(
                f"{'🟢' if direction == 'LONG' else '🔴'} {direction} @ ${entry:,.2f} | "
                f"Current: ${current_price:,.2f} | Unrealized: ${unreal:+.2f}"
            )

    def _close_position(self, pos: dict, exit_price: float, reason: str):
        """Close a simulated position and record the trade."""
        direction = pos["direction"]
        entry = pos["entry_price"]
        qty = pos["quantity"]
        pos_id = pos["id"]
        sl = pos["sl_price"]
        tp = pos["tp_price"]
        symbol = pos["symbol"]
        entry_time = pos["entry_time"]

        entry_config = pos.get("entry_config", {})
        slippage = float(entry_config.get("slippage_bps", config.get("execution.slippage_bps", 2.0))) / 10000.0
        fill_price = exit_price * (1.0 - slippage if direction == "LONG" else 1.0 + slippage)
        fee_rate = float(entry_config.get("fee_rate_per_side", config.get("execution.fee_rate_per_side", FEE_RATE)))
        gross_pnl, fee, net_pnl = calc_pnl(direction, entry, fill_price, qty, fee_rate)

        # Calculate duration
        try:
            et = datetime.strptime(entry_time, "%Y-%m-%d %H:%M:%S")
            duration_min = int((now_ist() - et).total_seconds() / 60)
        except Exception:
            duration_min = 0

        exit_time = now_ist_str()

        # Record in DB (also updates balance and daily stats)
        delta_order_id = entry_config.get("delta_entry_order_id")
        execution_venue = entry_config.get(
            "execution_venue", "DELTA_TESTNET" if delta_order_id else "PAPER_ONLY")
        initial_risk = float(pos.get("initial_risk_points") or abs(entry - float(pos.get("initial_sl_price") or sl)))
        target_rrr = round(gross_pnl / (initial_risk * qty), 4) if initial_risk > 0 and qty > 0 else 0.0

        trade_id = record_trade(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            exit_price=fill_price,
            quantity=qty,
            sl_price=sl,
            tp_price=tp,
            gross_pnl=gross_pnl,
            fee=fee,
            net_pnl=net_pnl,
            entry_time=entry_time,
            exit_time=exit_time,
            duration_min=duration_min,
            exit_reason=reason,
            signal_reason=pos.get("signal_reason", ""),
            signal_id=pos.get("signal_id"),
            execution_venue=execution_venue,
            delta_order_id=delta_order_id,
            target_rrr=target_rrr,
        )

        _excel_exporter.queue_export({
            "trade_id": trade_id,
            "symbol": symbol,
            "execution_venue": execution_venue,
            "delta_order_id": delta_order_id or "",
            "direction": direction,
            "entry_time": entry_time,
            "exit_time": exit_time,
            "entry_price": entry,
            "exit_price": fill_price,
            "quantity": qty,
            "sl_price": sl,
            "tp_price": tp,
            "target_rrr": target_rrr,
            "realized_pnl": net_pnl,
            "exit_reason": reason,
        })

        close_position(pos_id)

        emoji = "✅" if net_pnl > 0 else "🛑"
        logger.info(
            f"\n{'='*65}\n"
            f"{emoji} {direction} CLOSED  [{reason}]  [ID:{pos_id}]\n"
            f"  Entry:   ${entry:,.2f}\n"
            f"  Exit:    ${fill_price:,.2f}\n"
            f"  Qty:     {qty} BTC\n"
            f"  Gross:   ${gross_pnl:+.4f}\n"
            f"  Fee:     -${fee:.4f}\n"
            f"  Net:     ${net_pnl:+.4f}\n"
            f"  Duration:{duration_min} min\n"
            f"{'='*65}"
        )

        state = get_state()
        log_event("INFO", "engine",
                  f"{direction} closed | {reason} | net P&L=${net_pnl:+.4f} (fee {fee_rate:.4%}/side) | "
                  f"Balance=${state.get('balance', 0):.2f}")

    # ------------------------------------------------------------------ #
    # Stop
    # ------------------------------------------------------------------ #

    def stop(self):
        """Gracefully stop the engine."""
        logger.info("Engine stopping...")
        self.running = False
        self._stop_event.set()
        backtest_engine = getattr(self, "_backtest_engine", None)
        if backtest_engine:
            backtest_engine.stop()
        _excel_exporter.close()
        if config.get("execution.mode", "paper") == "delta_demo":
            self._delta_ready = False
            status = get_exchange_status()
            status.update(ready=False, message="Trading engine stopped; exchange-side protective orders remain active")
            update_exchange_status(status)
        update_state(engine_status="stopped")
        log_event("INFO", "engine", "Engine stopped gracefully")
        release_lock()


# ====================================================================== #
# Entry point
# ====================================================================== #

def main():
    # Single-instance check
    try:
        import psutil
    except ImportError:
        psutil = None

    if not acquire_lock():
        print("\n⚠️  Paper Engine is already running!")
        print("   Ek hi instance run kar sakta hai ek samay mein.")
        print("   Pehle wala process band karo, phir dubara start karo.\n")
        sys.exit(1)

    engine = PaperEngine()

    # Graceful shutdown on Ctrl+C or SIGTERM
    def _shutdown(sig, frame):
        print("\n\n🛑 Shutdown signal received. Stopping engine...")
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        engine.start()
    except KeyboardInterrupt:
        engine.stop()
    finally:
        release_lock()


if __name__ == "__main__":
    main()
