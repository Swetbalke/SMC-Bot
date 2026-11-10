"""
paper_engine.py — Offline Paper Trading Simulator
Complete rewrite — all bugs fixed.

Koi exchange connection nahi. Koi API keys nahi.
Delta Exchange PUBLIC candle data ya CSV use karta hai.
"""

import sys
import os
import time
import signal
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

# --- Project imports ---
from config_manager import config
from logger_setup import get_logger
from database import (
    init_db, get_state, update_state, check_and_reset_daily,
    save_position, get_open_positions, close_position,
    record_trade, log_signal, log_event, get_performance_summary
)
from data_client import DeltaPublicClient, generate_sample_csv
from strategy import detect_signals, calc_sl_tp, calc_quantity
from indicators import apply_all_indicators

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
    return datetime.utcnow() + IST_OFFSET


def now_ist_str() -> str:
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")


# ====================================================================== #
# P&L Calculator
# ====================================================================== #

FEE_RATE = 0.0005  # 0.05% per side (Delta maker/taker approximate)


def calc_pnl(direction: str, entry: float, exit_price: float, qty: float):
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
    fee = round((entry * qty * FEE_RATE) + (exit_price * qty * FEE_RATE), 4)
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

    # ------------------------------------------------------------------ #
    # Startup
    # ------------------------------------------------------------------ #

        def start(self):
        """Initialize everything and start the main loop."""
        logger.info("=" * 70)
        logger.info("PAPER TRADING ENGINE - STARTING")
        logger.info("=" * 70)

        # 1. Initialize database before writing database events
        try:
            init_db()
            log_event("INFO", "engine", "Engine starting")
            check_and_reset_daily()
            logger.info("Database initialized")
            log_event("INFO", "engine", "Database initialized")
        except Exception as e:
            logger.error(
                f"Database init failed: {e}",
                exc_info=True
            )
            sys.exit(1)

        # 2. Generate sample file for legacy compatibility
        generate_sample_csv()

        # 3. Initialize data client
        fallback_csv = config.get(
            "data.fallback_csv",
            "data/btc_1m_sample.csv"
        )
        self._data_client = DeltaPublicClient(
            fallback_csv=fallback_csv
        )

        # 4. Test data connection
        self._test_data_connection()

        # 5. Load persisted state
        state = get_state()
        balance = state.get("balance", 1000.0)
        daily_trades = state.get("daily_trades", 0)

        logger.info(
            f"State restored - Balance: ${balance:.2f} "
            f"| Daily Trades: {daily_trades}"
        )
        log_event(
            "INFO",
            "engine",
            f"State restored. Balance=${balance:.2f}"
        )

        # 6. Mark engine as running
        self.running = True
        update_state(
            engine_status="running",
            engine_heartbeat=now_ist_str()
        )
        log_event(
            "INFO",
            "engine",
            "Engine running - paper simulator active"
        )

        # 7. Start main loop
        self._main_loop()


    def _test_data_connection(self):
        """Test data source and report status."""
        symbol = config.get("general.symbol", "BTCUSDT")
        df = self._data_client.get_candles(symbol, limit=10)

        if df is not None and not df.empty:
            price = df["close"].iloc[-1]
            source = "CSV fallback" if self._data_client.is_using_fallback() else "Delta Exchange API"
            logger.info(f"✅ Data source: {source} | {symbol} @ ${price:,.2f}")
            log_event("INFO", "engine", f"Data connected via {source}. {symbol}=${price:,.2f}")
        else:
            err = self._data_client.get_last_error()
            logger.warning(f"⚠️ Data unavailable: {err}. Will retry.")
            log_event("WARNING", "engine", f"Data unavailable: {err}")

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
        last_pos_check = 0.0

        logger.info(f"🔄 Main loop started — analysis every {analysis_interval}s, "
                    f"position check every {pos_check_interval}s")

        while not self._stop_event.is_set():
            loop_start = time.time()

            try:
                # Update heartbeat
                update_state(engine_heartbeat=now_ist_str())

                # Daily reset check
                check_and_reset_daily()

                if not self.paused:
                    # --- Analysis cycle ---
                    self._run_analysis()

                    # --- Position management (more frequent) ---
                    now = time.time()
                    if (now - last_pos_check) >= pos_check_interval:
                        self._manage_positions()
                        last_pos_check = now

                # Hot-reload config
                config.reload()
                analysis_interval = config.get("processing.analysis_interval_sec", 60)
                pos_check_interval = config.get("processing.position_check_interval_sec", 30)

            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
                log_event("ERROR", "engine", f"Main loop error: {e}")
                time.sleep(5)
                continue

            # Sleep until next cycle
            elapsed = time.time() - loop_start
            sleep_time = max(5.0, analysis_interval - elapsed)
            logger.debug(f"Cycle done in {elapsed:.1f}s, sleeping {sleep_time:.1f}s")
            self._stop_event.wait(timeout=sleep_time)

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
            update_state(engine_status="data_unavailable")
            return

        # Apply indicators
        cfg_ind = config.get_section("indicators")
        df = apply_all_indicators(df, cfg_ind)
        
        # Check for new closed candle (avoid processing same candle twice)
        if len(df) < 2:
            logger.warning("Not enough candles received")
            return

        signal_candle = df.iloc[-2]  # last CLOSED candle
        candle_ts = str(signal_candle["time"])

        if candle_ts == self._last_candle_ts:
            logger.debug(f"Same candle ({candle_ts}) — skipping")
            return

        self._last_candle_ts = candle_ts

        price = float(df.iloc[-1]["close"])  # live price from forming candle

        logger.info(f"📊 New candle: {candle_ts} | {symbol} @ ${price:,.2f}")
        update_state(last_candle_ts=candle_ts, engine_status="running")

        # Check if we can open new positions
        state = get_state()
        open_pos = get_open_positions()
        daily_trades = state.get("daily_trades", 0)
        daily_pnl = state.get("daily_pnl", 0.0)
        balance = state.get("balance", 1000.0)

        max_daily_trades = config.get("entry.max_daily_trades", 5)
        max_daily_loss_pct = config.get("risk.max_daily_loss_pct", 5.0)
        max_positions = config.get("risk.max_open_positions", 1)

        # Daily loss limit check
        capital = config.get("risk.capital", 1000.0)
        daily_loss_limit = capital * (max_daily_loss_pct / 100.0)

        if daily_pnl <= -daily_loss_limit:
            logger.warning(f"🛑 Daily loss limit reached (${daily_pnl:.2f}). No new entries.")
            return

        if daily_trades >= max_daily_trades:
            logger.info(f"📌 Max daily trades reached ({daily_trades}/{max_daily_trades}). No new entries.")
            return

        if len(open_pos) >= max_positions:
            logger.debug(f"Max positions open ({len(open_pos)}/{max_positions}).")
            return

        # Detect signal
        cfg_entry = config.get_section("entry")
        signal, reason = detect_signals(df, cfg_ind, cfg_entry)

        if signal:
            logger.info(f"🚦 Signal: {signal} | {reason}")
            log_signal(symbol, signal, price, reason, acted_upon=False)
            self._open_position(signal, price, reason, balance, now_ist_str())
        else:
            logger.debug(f"No signal | {symbol} @ ${price:,.2f}")

    # ------------------------------------------------------------------ #
    # Open position
    # ------------------------------------------------------------------ #

    def _open_position(self, signal: str, price: float, reason: str,
                       balance: float, entry_time: str):
        """Create a new simulated position."""
        cfg_exit = config.get_section("exit")
        cfg_risk = config.get_section("risk")
        symbol = config.get("general.symbol", "BTCUSDT")

        sl_price, tp_price = calc_sl_tp(signal, price, cfg_exit)
        quantity = calc_quantity(price, sl_price, cfg_risk)

        pos_id = save_position(
            symbol=symbol,
            direction=signal,
            entry_price=price,
            quantity=quantity,
            sl_price=sl_price,
            tp_price=tp_price,
            entry_time=entry_time,
            signal_reason=reason
        )

        sl_pct = abs(price - sl_price) / price * 100
        tp_pct = abs(tp_price - price) / price * 100
        risk_amt = round(quantity * abs(price - sl_price), 2)

        logger.info(
            f"\n{'='*65}\n"
            f"{'🟢 LONG' if signal == 'LONG' else '🔴 SHORT'} POSITION OPENED  [ID:{pos_id}]\n"
            f"  Symbol:   {symbol}\n"
            f"  Entry:    ${price:,.2f}\n"
            f"  Qty:      {quantity} BTC\n"
            f"  SL:       ${sl_price:,.2f} ({sl_pct:.2f}%)\n"
            f"  TP:       ${tp_price:,.2f} ({tp_pct:.2f}%)\n"
            f"  Risk:     ${risk_amt:.2f}\n"
            f"  Signal:   {reason}\n"
            f"{'='*65}"
        )
        log_event("INFO", "engine",
                  f"{signal} opened @ ${price:,.2f} | qty={quantity} | SL=${sl_price:.2f} | TP=${tp_price:.2f} | {reason}")
        log_signal(symbol, signal, price, reason, acted_upon=True)

    # ------------------------------------------------------------------ #
    # Position management
    # ------------------------------------------------------------------ #

    def _manage_positions(self):
        """Check all open positions for SL/TP hits."""
        open_pos = get_open_positions()
        if not open_pos:
            return

        symbol = config.get("general.symbol", "BTCUSDT")
        price = self._data_client.get_current_price(symbol)

        if price is None:
            logger.warning("Cannot manage positions — price unavailable")
            return

        for pos in open_pos:
            self._check_exit(pos, price)

    def _check_exit(self, pos: dict, current_price: float):
        """Check if SL or TP is hit for a position."""
        direction = pos["direction"]
        entry = pos["entry_price"]
        sl = pos["sl_price"]
        tp = pos["tp_price"]
        qty = pos["quantity"]
        pos_id = pos["id"]
        entry_time = pos["entry_time"]

        exit_reason = None

        if direction == "LONG":
            if current_price <= sl:
                exit_reason = "SL Hit"
            elif current_price >= tp:
                exit_reason = "TP Hit"
        else:  # SHORT
            if current_price >= sl:
                exit_reason = "SL Hit"
            elif current_price <= tp:
                exit_reason = "TP Hit"

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

        gross_pnl, fee, net_pnl = calc_pnl(direction, entry, exit_price, qty)

        # Calculate duration
        try:
            et = datetime.strptime(entry_time, "%Y-%m-%d %H:%M:%S")
            duration_min = int((now_ist() - et).total_seconds() / 60)
        except Exception:
            duration_min = 0

        exit_time = now_ist_str()

        # Record in DB (also updates balance and daily stats)
        record_trade(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            exit_price=exit_price,
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
            signal_reason=pos.get("signal_reason", "")
        )

        close_position(pos_id)

        emoji = "✅" if net_pnl > 0 else "🛑"
        logger.info(
            f"\n{'='*65}\n"
            f"{emoji} {direction} CLOSED  [{reason}]  [ID:{pos_id}]\n"
            f"  Entry:   ${entry:,.2f}\n"
            f"  Exit:    ${exit_price:,.2f}\n"
            f"  Qty:     {qty} BTC\n"
            f"  Gross:   ${gross_pnl:+.4f}\n"
            f"  Fee:     -${fee:.4f}\n"
            f"  Net:     ${net_pnl:+.4f}\n"
            f"  Duration:{duration_min} min\n"
            f"{'='*65}"
        )

        state = get_state()
        log_event("INFO", "engine",
                  f"{direction} closed | {reason} | net P&L=${net_pnl:+.4f} | "
                  f"Balance=${state.get('balance', 0):.2f}")

    # ------------------------------------------------------------------ #
    # Stop
    # ------------------------------------------------------------------ #

    def stop(self):
        """Gracefully stop the engine."""
        logger.info("Engine stopping...")
        self.running = False
        self._stop_event.set()
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
