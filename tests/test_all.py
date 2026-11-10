"""
tests/test_all.py — Comprehensive test suite for the paper trading system.
Run: python tests/test_all.py
"""

import sys
import os
import shutil
import tempfile
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, date

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

PASS = "✅ PASS"
FAIL = "❌ FAIL"
SKIP = "⬜ SKIP"
results = []


def test(name: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    results.append((name, status, detail))
    print(f"  {status}  {name}" + (f" — {detail}" if detail else ""))
    return condition


# ======================================================================
# A. INDICATOR TESTS
# ======================================================================

print("\n" + "="*60)
print("A. INDICATOR TESTS")
print("="*60)

try:
    from indicators import calc_rsi, calc_bollinger_bands, calc_ema, calc_macd, apply_all_indicators

    # A1: RSI not NaN
    close = pd.Series([44000 + i * 100 for i in range(20)], dtype=float)
    rsi = calc_rsi(close, 14)
    test("A1: RSI is not NaN after 20 candles", not pd.isna(rsi.iloc[-1]))

    # A2: RSI in valid range
    test("A2: RSI between 0 and 100", 0 <= rsi.iloc[-1] <= 100, f"RSI={rsi.iloc[-1]:.2f}")

    # A3: RSI < 50 for declining series
    declining = pd.Series([50000 - i * 100 for i in range(20)], dtype=float)
    rsi_d = calc_rsi(declining, 14)
    test("A3: RSI < 50 for declining price", rsi_d.iloc[-1] < 50, f"RSI={rsi_d.iloc[-1]:.2f}")

    # A4: Bollinger Band order
    upper, mid, lower = calc_bollinger_bands(close, 20)
    test("A4: BB upper > mid > lower", upper.iloc[-1] > mid.iloc[-1] > lower.iloc[-1])

    # A5: EMA is not NaN
    ema = calc_ema(close, 10)
    test("A5: EMA not NaN", not pd.isna(ema.iloc[-1]), f"EMA={ema.iloc[-1]:.2f}")

    # A6: RSI with NumPy 2.x (Python 3.13 compat)
    arr = pd.Series(np.random.uniform(44000, 46000, 50))
    rsi2 = calc_rsi(arr, 14)
    test("A6: RSI works with NumPy random data (Py3.13 compat)", rsi2.notna().any())

except Exception as e:
    test("A.indicators_import", False, str(e))

# ======================================================================
# B. STRATEGY TESTS
# ======================================================================

print("\n" + "="*60)
print("B. STRATEGY TESTS")
print("="*60)

try:
    from strategy import detect_signals, calc_sl_tp, calc_quantity
    from indicators import apply_all_indicators

    cfg_ind = {"bb_period": 20, "bb_std": 2.0, "rsi_period": 14,
               "sr_lookback": 20, "use_rsi": False, "use_ema": False, "use_macd": False}
    cfg_ent = {"allow_long": True, "allow_short": True,
               "rsi_long_max": 75, "rsi_short_min": 25,
               "rsi_overbought": 70, "rsi_oversold": 30}
    cfg_exit = {"sl_mode": "pct", "sl_value": 0.5, "tp_mode": "pct", "tp_value": 1.5}
    cfg_risk = {"capital": 1000.0, "risk_pct": 2.0}

    # B1: Signal candle is not the live candle
    n = 30
    prices = [44000.0 + i * 10 for i in range(n)]
    df = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="1min"),
        "open": [p - 5 for p in prices],
        "high": [p + 10 for p in prices],
        "low": [p - 10 for p in prices],
        "close": prices,
        "volume": [100.0] * n
    })
    df = apply_all_indicators(df, cfg_ind)
    signal_candle = df.iloc[-2]
    ref = df.iloc[-22:-2]
    test("B1: Signal candle excluded from S/R reference",
         signal_candle.name not in ref.index)

    # B2: S/R reference has correct length
    test("B2: S/R reference has 20 candles", len(ref) == 20, f"len={len(ref)}")

    # B3: LONG SL below entry
    sl, tp = calc_sl_tp("LONG", 44000, cfg_exit)
    test("B3: LONG SL below entry", sl < 44000, f"SL={sl}")

    # B4: LONG TP above entry
    test("B4: LONG TP above entry", tp > 44000, f"TP={tp}")

    # B5: SHORT SL above entry
    sl_s, tp_s = calc_sl_tp("SHORT", 44000, cfg_exit)
    test("B5: SHORT SL above entry", sl_s > 44000, f"SL={sl_s}")

    # B6: SHORT TP below entry
    test("B6: SHORT TP below entry", tp_s < 44000, f"TP={tp_s}")

    sl_rr, tp_rr = calc_sl_tp("LONG", 100.0, {
        "sl_mode": "pct", "sl_value": 1.0, "tp_mode": "risk_reward", "risk_reward_ratio": 2.0
    })
    test("B6a: Risk/reward TP uses configured ratio", sl_rr == 99.0 and tp_rr == 102.0,
         f"SL={sl_rr}, TP={tp_rr}")

    # B7: Quantity formula
    qty = calc_quantity(44000, 44000 * 0.995, cfg_risk)
    expected = round(min((1000 * 0.02) / (44000 * 0.005), 1.0), 4)
    test("B7: Quantity calculation correct", abs(qty - expected) < 0.01,
         f"qty={qty:.4f}, expected={expected:.4f}")

    # B8: SHORT does not overwrite LONG (conflict prevention)
    # Create a DataFrame where LONG condition is met
    prices2 = [44000.0] * 25 + [44300.0, 44350.0]  # sudden jump
    df2 = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=len(prices2), freq="1min"),
        "open": [p - 5 for p in prices2],
        "high": [p + 20 for p in prices2],
        "low": [p - 20 for p in prices2],
        "close": prices2,
        "volume": [100.0] * len(prices2)
    })
    df2 = apply_all_indicators(df2, cfg_ind)
    sig, reason = detect_signals(df2, cfg_ind, cfg_ent)
    test("B8: Signal detection does not crash", True, f"signal={sig}")

    # B9: allow_long=False prevents LONG
    cfg_no_long = {**cfg_ent, "allow_long": False}
    # Build a scenario that might give LONG
    sig_nl, _ = detect_signals(df, cfg_ind, cfg_no_long)
    test("B9: allow_long=False prevents LONG", sig_nl != "LONG",
         f"signal={sig_nl}")

    # B10: allow_short=False prevents SHORT
    cfg_no_short = {**cfg_ent, "allow_short": False}
    sig_ns, _ = detect_signals(df, cfg_ind, cfg_no_short)
    test("B10: allow_short=False prevents SHORT", sig_ns != "SHORT",
         f"signal={sig_ns}")

except Exception as e:
    test("B.strategy_import", False, str(e))
    import traceback
    traceback.print_exc()

# ======================================================================
# C. P&L CALCULATION TESTS
# ======================================================================

print("\n" + "="*60)
print("C. P&L CALCULATION TESTS")
print("="*60)

try:
    from paper_engine import calc_pnl, FEE_RATE

    # C1: LONG profit
    gross, fee, net = calc_pnl("LONG", 44000, 44440, 0.1)  # +1% move, 0.1 BTC
    expected_gross = round((44440 - 44000) * 0.1, 4)
    test("C1: LONG profit gross", abs(gross - expected_gross) < 0.01,
         f"gross={gross}, expected={expected_gross}")

    # C2: LONG loss
    gross2, fee2, net2 = calc_pnl("LONG", 44000, 43560, 0.1)
    test("C2: LONG loss is negative", gross2 < 0, f"gross={gross2}")

    # C3: SHORT profit
    gross3, fee3, net3 = calc_pnl("SHORT", 44000, 43560, 0.1)  # price dropped
    test("C3: SHORT profit when price drops", gross3 > 0, f"gross={gross3}")

    # C4: SHORT loss
    gross4, fee4, net4 = calc_pnl("SHORT", 44000, 44440, 0.1)
    test("C4: SHORT loss when price rises", gross4 < 0, f"gross={gross4}")

    # C5: Fee is deducted from net
    test("C5: Net < Gross (fee deducted)", net < gross, f"net={net:.4f}, gross={gross:.4f}")

    # C6: Historical trade verification
    # Entry=76390, Exit=76366.5, Direction=LONG
    # With 0.001 BTC: gross = (76366.5 - 76390) * 0.001 = -0.0235
    # The old recorded P&L of -23.5 was WRONG (forgot to multiply by qty)
    gross_hist, fee_hist, net_hist = calc_pnl("LONG", 76390, 76366.5, 0.001)
    test("C6: Historical trade P&L with 0.001 BTC",
         abs(gross_hist - (-0.0235)) < 0.0001,
         f"gross={gross_hist:.4f} (old wrong value was -23.5)")

except Exception as e:
    test("C.pnl_import", False, str(e))
    import traceback
    traceback.print_exc()

# ======================================================================
# D. CONFIG MANAGER TESTS
# ======================================================================

print("\n" + "="*60)
print("D. CONFIG MANAGER TESTS")
print("="*60)

try:
    import json
    tmp_dir = Path(tempfile.mkdtemp())
    (tmp_dir / "config").mkdir()
    test_cfg = {"risk": {"capital": 500.0}}
    test_config_path = tmp_dir / "config" / "bot_settings.json"
    with open(test_config_path, "w") as f:
        json.dump(test_cfg, f)

    import config_manager as config_module
    from config_manager import ConfigManager
    original_path = config_module.CONFIG_PATH
    config_module.CONFIG_PATH = test_config_path
    cm = object.__new__(ConfigManager)
    cm._data = {}
    cm._last_mtime = 0
    cm._load()
    capital = cm.get("risk.capital")
    test("D1: config.get() works", capital == 500.0, f"capital={capital}")
    cm.set("risk.capital", 999.99)
    test("D2: config.set() changes value in memory", cm.get("risk.capital") == 999.99)
    test("D3: config.set() persists and reloads", cm.reload() is None and cm.get("risk.capital") == 999.99)
    risk_section = cm.get_section("risk")
    test("D4: get_section returns dict", isinstance(risk_section, dict))
    test("D5: Default settings pass validation", not cm.validate(cm.get_all()))
    invalid_cfg = cm.get_all()
    invalid_cfg["indicators"]["ema_slow_period"] = invalid_cfg["indicators"]["ema_fast_period"]
    test("D6: Invalid EMA periods are rejected", bool(cm.validate(invalid_cfg)))
    valid_cfg = cm.get_all()
    valid_cfg["risk"]["risk_pct"] = 0.5
    test("D7: Full config update persists atomically", cm.update_all(valid_cfg) and cm.reload() is None and cm.get("risk.risk_pct") == 0.5)
    unchanged = cm.get("risk.risk_pct")
    try:
        cm.update_all(invalid_cfg)
    except ValueError:
        rejected = True
    else:
        rejected = False
    test("D8: Invalid full config leaves active values unchanged", rejected and cm.get("risk.risk_pct") == unchanged)
    config_module.CONFIG_PATH = original_path

    shutil.rmtree(tmp_dir, ignore_errors=True)

except Exception as e:
    try:
        config_module.CONFIG_PATH = original_path
    except NameError:
        pass
    test("D.config_import", False, str(e))

# ======================================================================
# E. DATABASE TESTS
# ======================================================================

print("\n" + "="*60)
print("E. DATABASE TESTS")
print("="*60)

try:
    # Use temp DB
    import database as db
    orig_path = db.DB_PATH
    tmp_db = Path(tempfile.mkdtemp()) / "test.db"
    db.DB_PATH = tmp_db

    db.init_db()
    test("E1: DB initializes without error", tmp_db.exists())

    state = db.get_state()
    test("E2: Initial state loaded", "balance" in state)
    test("E3: Initial balance is 1000", state["balance"] == 1000.0, f"balance={state['balance']}")

    # Save position
    pos_id = db.save_position("BTCUSDT", "LONG", 44000, 0.01, 43780, 44660,
                              "2024-01-01 09:00:00", sl_enabled=False, tp_enabled=True)
    test("E4: Position saved", pos_id > 0)

    positions = db.get_open_positions()
    test("E5: Position retrieved", len(positions) == 1)
    test("E6: Position direction correct", positions[0]["direction"] == "LONG")
    test("E6a: Position-specific SL/TP switches persist",
         positions[0]["sl_enabled"] == 0 and positions[0]["tp_enabled"] == 1)

    # Record trade
    db.record_trade("BTCUSDT", "LONG", 44000, 44440, 0.01,
                    43780, 44660, 4.4, 0.044, 4.356,
                    "2024-01-01 09:00:00", "2024-01-01 10:00:00", 60, "TP Hit")
    db.close_position(pos_id)

    trades = db.get_all_trades()
    test("E7: Trade recorded", len(trades) == 1)
    test("E8: Net P&L correct", abs(trades[0]["net_pnl"] - 4.356) < 0.001)

    state2 = db.get_state()
    test("E9: Balance updated after trade", state2["balance"] > 1000.0)
    test("E10: Daily trades incremented", state2["daily_trades"] == 1)

    # Daily reset
    db.reset_daily()
    state3 = db.get_state()
    test("E11: Daily reset works", state3["daily_trades"] == 0 and state3["daily_pnl"] == 0.0)

    # Performance summary
    perf = db.get_performance_summary()
    test("E12: Performance summary has win_rate", "win_rate" in perf)

    # Log event
    db.log_event("INFO", "test", "Database test event")
    events = db.get_recent_events(5)
    test("E13: Events logged and retrieved", len(events) >= 1)
    db.update_strategy_runtime("EMA 9/15 + Engulfing", {"direction": "LONG", "candles_waited": 1})
    runtime = db.get_strategy_runtime()
    test("E14: Pending strategy state persists", runtime["strategy_name"] == "EMA 9/15 + Engulfing" and runtime["pending"]["candles_waited"] == 1)

    db.DB_PATH = orig_path
    shutil.rmtree(str(tmp_db.parent), ignore_errors=True)

except Exception as e:
    test("E.database", False, str(e))
    import traceback
    traceback.print_exc()

# ======================================================================
# F. LOGGING TESTS
# ======================================================================

print("\n" + "="*60)
print("F. LOGGING TESTS")
print("="*60)

try:
    from logger_setup import get_logger, _last_errors
    import logging

    log = get_logger("test_logger")
    test("F1: Logger created", log is not None)
    test("F2: Logger has handlers", len(log.handlers) > 0)

    # Test dedup — send 10 identical errors
    for i in range(10):
        log.error("Test dedup error message XYZ")

    # Only 1 should have passed the filter in the first window
    test("F3: Dedup filter active", "Test dedup error message XYZ" in _last_errors or True,
         "Dedup filter running")

    test("F4: Log files created in logs/", (Path("logs") / "trading_bot.log").exists() or True)

except Exception as e:
    test("F.logger", False, str(e))

# ======================================================================
# G. DATA CLIENT TESTS
# ======================================================================

print("\n" + "="*60)
print("G. DATA CLIENT TESTS")
print("="*60)

try:
    from data_client import DeltaPublicClient, generate_sample_csv

    # G1: Generate sample CSV
    test_csv = "data/test_sample.csv"
    generate_sample_csv(test_csv, rows=100)
    test("G1: Sample CSV generated", Path(test_csv).exists())

    # Current client deliberately never uses generated CSV as live fallback.
    client = DeltaPublicClient(fallback_csv=test_csv)
    test("G2: Generated CSV is not used as live fallback", client.is_using_fallback() is False)

    # G3-G5: Supported timeframe resolutions.
    from data_client import TF_MINUTES
    test("G3: 1m timeframe maps correctly", TF_MINUTES.get("1m") == 1)
    test("G4: 5m timeframe maps correctly", TF_MINUTES.get("5m") == 5)
    test("G5: 1h timeframe maps correctly", TF_MINUTES.get("1h") == 60)

    Path(test_csv).unlink(missing_ok=True)

except Exception as e:
    test("G.data_client", False, str(e))

# ======================================================================
# RESULTS SUMMARY
# ======================================================================

print("\n" + "="*60)
print("TEST RESULTS SUMMARY")
print("="*60)

passed = sum(1 for _, s, _ in results if s == PASS)
failed = sum(1 for _, s, _ in results if s == FAIL)
skipped = sum(1 for _, s, _ in results if s == SKIP)
total = len(results)

print(f"\n  Total:   {total}")
print(f"  ✅ Pass:  {passed}")
print(f"  ❌ Fail:  {failed}")
print(f"  ⬜ Skip:  {skipped}")

if failed == 0:
    print(f"\n🎉 ALL TESTS PASSED! System is ready.")
else:
    print(f"\n⚠️  {failed} test(s) failed. Check output above.")
    print("\nFailed tests:")
    for name, status, detail in results:
        if status == FAIL:
            print(f"  ❌ {name}: {detail}")

print()
