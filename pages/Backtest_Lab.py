"""Streamlit page for the isolated Backtest Lab."""
import json
import time
from datetime import datetime, timedelta, timezone
import pandas as pd
import streamlit as st
from backtest_lab.jobs import start, cancel
from backtest_lab.storage import active_job, get_job, recent_runs
from backtest_lab.strategy_registry import STRATEGIES

st.set_page_config(page_title="Backtest Lab", layout="wide")
st.title("Backtest Lab")
st.info("Backtest Lab: separate from paper trading, uses public data only.")

def local_config(strategy, capital, risk_pct, ema_fast, ema_slow, sl_points, rr):
    return {"general": {"strategy_name": strategy},
      "indicators": {"ema_fast_period": ema_fast, "ema_slow_period": ema_slow,
          "ema_crossover_enabled": True, "ema_signal_mode": "crossover_only", "ema_sl_mode": "fixed_points",
          "bb_period": 20, "bb_std": 2, "rsi_period": 14, "sr_lookback": 20, "use_rsi": False, "use_ema": False},
      "entry": {"allow_long": True, "allow_short": True, "trade_direction": "both", "rsi_long_max": 75, "rsi_short_min": 25},
      "exit": {"sl_mode": "fixed", "sl_value": sl_points, "sl_points": sl_points},
      "risk": {"capital": capital, "risk_pct": risk_pct, "minimum_position_size": .0001, "maximum_position_size": 1,
          "risk_reward_ratio": rr}}

with st.form("backtest_request"):
    c1, c2, c3, c4 = st.columns(4)
    strategy = c1.selectbox("Strategy", STRATEGIES)
    symbol = c2.selectbox("Symbol", ["BTCUSD", "ETHUSD"])
    timeframe = c3.selectbox("Timeframe", ["1m", "3m", "5m", "15m", "30m", "1h", "4h"])
    period = c4.selectbox("Range", ["Last 24 hours", "Last 7 days", "Last 30 days", "Last 90 days"])
    c5, c6, c7, c8 = st.columns(4)
    capital = c5.number_input("Capital ($)", 100.0, 1_000_000.0, 1000.0)
    risk_pct = c6.number_input("Risk per trade (%)", .1, 10.0, 2.0)
    fee = c7.number_input("Fee per side", 0.0, .02, .0005, format="%.4f")
    slippage = c8.number_input("Slippage (bps)", 0.0, 500.0, 2.0)
    c9, c10, c11, c12 = st.columns(4)
    ema_fast = c9.number_input("Fast EMA", 1, 199, 9)
    ema_slow = c10.number_input("Slow EMA", 2, 200, 15)
    sl_points = c11.number_input("Stop points", .01, 100000.0, 100.0)
    rr = c12.number_input("Risk reward", .1, 20.0, 3.0)
    run_clicked = st.form_submit_button("Run backtest")

if run_clicked:
    if ema_slow <= ema_fast:
        st.error("Slow EMA must be greater than Fast EMA.")
    else:
        hours = {"Last 24 hours": 24, "Last 7 days": 168, "Last 30 days": 720, "Last 90 days": 2160}[period]
        end = int(datetime.now(timezone.utc).timestamp() // 60 * 60)
        request = {"strategy": strategy, "symbol": symbol, "timeframe": timeframe, "start_ts": end - hours * 3600,
                   "end_ts": end, "costs": {"fee_rate": fee, "slippage_bps": slippage}, "in_sample_fraction": .7,
                   "strategy_config": local_config(strategy, capital, risk_pct, ema_fast, ema_slow, sl_points, rr)}
        try:
            st.session_state["backtest_job"] = start(request)
            st.success("Backtest job started.")
        except RuntimeError as exc:
            st.warning(str(exc))

job = active_job() or (get_job(st.session_state["backtest_job"]) if "backtest_job" in st.session_state else None)
if job:
    st.subheader("Job status")
    st.progress(int(job["progress"]))
    st.write(f"{job['status'].upper()} — {job['message'] or job.get('error') or 'Working'}")
    if job["status"] in {"queued", "running"} and st.button("Cancel"):
        cancel(job["id"]); st.rerun()

runs = recent_runs()
if runs:
    latest = runs[0]
    result = json.loads(latest["result_json"])
    st.subheader("Latest result")
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Trades", result["trades"]); m2.metric("Win rate", f"{result['win_rate']:.1f}%")
    m3.metric("Profit factor", "—" if result["profit_factor"] is None else f"{result['profit_factor']:.2f}")
    m4.metric("Max drawdown", f"{result['max_drawdown_pct']:.2f}%"); m5.metric("Sharpe", f"{result['sharpe']:.2f}")
    st.caption(result["sharpe_formula"])
    if result["trades"] < 30:
        st.warning("30 se kam trades hain; result statistically meaningful nahi hai.")
    if result["gaps"]:
        st.warning(f"Public candle data mein {result['gaps']} gap(s) mile.")
    st.subheader("In-sample vs out-of-sample")
    st.dataframe(pd.DataFrame([
        {"Window": "In sample", "Trades": result["in_sample"]["trades"], "Win rate": result["in_sample"]["win_rate"], "Return %": result["in_sample"]["total_return"]},
        {"Window": "Out of sample", "Trades": result["out_of_sample"]["trades"], "Win rate": result["out_of_sample"]["win_rate"], "Return %": result["out_of_sample"]["total_return"]},
    ]), hide_index=True)
    st.line_chart(pd.DataFrame({"equity": result["equity"]}))
    st.download_button("Download trade CSV", latest["trades_csv"], "backtest_trades.csv", "text/csv")
    st.subheader("Past runs")
    st.dataframe(pd.DataFrame([{"Run": r["id"], "Time": r["created_at"]} for r in runs]), hide_index=True)
