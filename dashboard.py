"""
dashboard.py — Professional 3-Page Trading Terminal
SMC Algo Paper Trading System
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import json
import html
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

# Project imports
from config_manager import config, DEFAULTS, EMA_TRAIL_STRATEGY, LEGACY_EMA_STRATEGY
from database import (
    get_state, get_open_positions, get_all_trades,
    get_performance_summary, get_recent_signals,
    get_recent_events, export_trades_csv, init_db,
    clear_all_positions, update_state, reset_daily, get_strategy_runtime,
    get_exchange_status, get_latest_backtest
)
from data_client import DeltaPublicClient

# ====================================================================== #
# PAGE CONFIG
# ====================================================================== #

st.set_page_config(
    page_title="SMC Algo Terminal",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={"About": "SMC Algo Trading Terminal — Paper and Delta Demo modes"}
)

# ====================================================================== #
# GLOBAL STYLES
# ====================================================================== #

st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

  /* Base */
  .stApp { background: #0a0e1a; color: #e2e8f0; font-family: 'Inter', sans-serif; }
  .main .block-container { padding: 1rem 1.5rem; max-width: 100%; }

  /* Sidebar */
  section[data-testid="stSidebar"] { background: #0f1629; border-right: 1px solid #1e2a45; }
  section[data-testid="stSidebar"] .stMarkdown h1,
  section[data-testid="stSidebar"] .stMarkdown h2,
  section[data-testid="stSidebar"] .stMarkdown h3 { color: #64b5f6; }

  /* Status badges */
  .status-running  { background:#0d4f2a; color:#4caf50; border:1px solid #4caf50; border-radius:6px; padding:4px 10px; font-size:12px; font-weight:600; }
  .status-stopped  { background:#4f1a0d; color:#ef5350; border:1px solid #ef5350; border-radius:6px; padding:4px 10px; font-size:12px; font-weight:600; }
  .status-paused   { background:#4f3d0d; color:#ffa726; border:1px solid #ffa726; border-radius:6px; padding:4px 10px; font-size:12px; font-weight:600; }
  .status-data_unavailable { background:#3a1a4f; color:#ce93d8; border:1px solid #ce93d8; border-radius:6px; padding:4px 10px; font-size:12px; font-weight:600; }

  /* Metric cards */
  div[data-testid="stMetric"] {
    background: linear-gradient(135deg, #111827, #1a2340);
    border: 1px solid #1e3a5f;
    border-radius: 10px;
    padding: 12px 16px;
    box-shadow: 0 4px 15px rgba(0,0,0,0.3);
  }
  div[data-testid="stMetric"] label { color: #94a3b8 !important; font-size: 11px !important; text-transform: uppercase; letter-spacing: 0.08em; }
  div[data-testid="stMetric"] [data-testid="stMetricValue"] { color: #f1f5f9 !important; font-size: 22px !important; font-weight: 700; font-family: 'JetBrains Mono', monospace; }

  /* Trade tables */
  .trade-long  { color: #4caf50; font-weight: 600; }
  .trade-short { color: #ef5350; font-weight: 600; }
  .pnl-pos { color: #4caf50; }
  .pnl-neg { color: #ef5350; }

  /* Section headers */
  .section-header {
    background: linear-gradient(90deg, #1e3a5f, transparent);
    border-left: 3px solid #2196f3;
    padding: 8px 14px;
    border-radius: 4px;
    margin: 12px 0 8px 0;
    font-weight: 600;
    font-size: 14px;
    color: #90caf9;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }

  /* Activity feed */
  .event-info    { border-left: 3px solid #2196f3; padding: 4px 10px; margin: 3px 0; background: #0d1b2e; border-radius: 2px; font-size: 12px; }
  .event-warning { border-left: 3px solid #ffa726; padding: 4px 10px; margin: 3px 0; background: #1a1200; border-radius: 2px; font-size: 12px; }
  .event-error   { border-left: 3px solid #ef5350; padding: 4px 10px; margin: 3px 0; background: #1a0000; border-radius: 2px; font-size: 12px; }

  /* Buttons */
  .stButton > button { border-radius: 8px; font-weight: 600; font-size: 13px; transition: all 0.2s; }
  .stButton > button:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(33,150,243,0.3); }

  /* Tabs */
  .stTabs [data-baseweb="tab-list"] { background: #111827; border-radius: 10px; padding: 4px; gap: 4px; }
  .stTabs [data-baseweb="tab"] { background: transparent; color: #64748b; border-radius: 8px; font-weight: 500; }
  .stTabs [aria-selected="true"] { background: linear-gradient(135deg,#1565c0,#1976d2) !important; color: white !important; }

  /* Inputs */
  .stTextInput input, .stNumberInput input, .stSelectbox select { background: #111827 !important; color: #e2e8f0 !important; border: 1px solid #1e3a5f !important; border-radius: 8px !important; }

  /* Dataframe */
  .stDataFrame { border-radius: 8px; overflow: hidden; }
</style>
""", unsafe_allow_html=True)

# ====================================================================== #
# INIT
# ====================================================================== #

try:
    init_db()
except Exception as exc:
    st.error(f"Dashboard database initialization failed: {type(exc).__name__}: {exc}")

if "data_client" not in st.session_state:
    fallback = config.get("data.fallback_csv", "data/btc_1m_sample.csv")
    # Keep the chart's public market feed on the same Delta environment as the
    # configured execution mode, even when the dashboard is launched directly.
    data_env = "testnet" if config.get("execution.mode", "paper") == "delta_demo" else None
    st.session_state.data_client = DeltaPublicClient(fallback_csv=fallback, env=data_env)


def get_market_data_client():
    """Return a public data client aligned with the active execution mode."""
    desired_env = ("testnet" if config.get("execution.mode", "paper") == "delta_demo"
                   else os.environ.get("DELTA_ENV"))
    client = st.session_state.data_client
    if desired_env and getattr(client, "env", None) != desired_env:
        fallback = config.get("data.fallback_csv", "data/btc_1m_sample.csv")
        client = DeltaPublicClient(fallback_csv=fallback, env=desired_env)
        st.session_state.data_client = client
    return client


# ====================================================================== #
# HELPER FUNCTIONS
# ====================================================================== #

def fmt_pnl(val: float) -> str:
    sign = "+" if val >= 0 else ""
    return f"{sign}${val:.4f}"


def status_badge(status: str) -> str:
    return f'<span class="status-{status}">{status.upper().replace("_", " ")}</span>'


def color_pnl(val: float) -> str:
    cls = "pnl-pos" if val >= 0 else "pnl-neg"
    return f'<span class="{cls}">{fmt_pnl(val)}</span>'


def get_execution_positions(symbol=None) -> list:
    """Return local paper positions; Delta mirroring never owns trade state."""
    positions = get_open_positions()
    if symbol:
        positions = [p for p in positions if p.get("symbol") == symbol]
    return positions


def get_live_price() -> tuple:
    """Returns (price, source_label)"""
    client = get_market_data_client()
    symbol = config.get("general.symbol", "BTCUSDT")
    price = client.get_current_price(symbol)
    source = "CSV" if client.is_using_fallback() else (
        "Delta Demo/Testnet" if getattr(client, "env", "production") == "testnet"
        else "Delta Production"
    )
    return price, source


def get_candle_df(limit=200) -> pd.DataFrame:
    client = get_market_data_client()
    symbol = config.get("general.symbol", "BTCUSDT")
    tf = config.get("data.timeframe", "1m")
    df = client.get_candles(symbol, tf, limit)
    return df if df is not None else pd.DataFrame()


def build_candlestick_chart(df: pd.DataFrame, positions: list, title: str = "", market_price=None) -> go.Figure:
    """Build a Plotly candlestick chart with BB bands and signals."""
    if df is None or df.empty:
        fig = go.Figure()
        fig.update_layout(template="plotly_dark", title="Candles unavailable", height=400)
        return fig

    # Plotly candlesticks require valid, ordered timestamps and numeric OHLC.
    # Filter malformed rows so one bad candle cannot blank the whole chart.
    df = df.copy()
    required = {"time", "open", "high", "low", "close"}
    if not required.issubset(df.columns):
        fig = go.Figure()
        missing = ", ".join(sorted(required - set(df.columns)))
        fig.update_layout(template="plotly_dark", title=f"Chart data missing: {missing}", height=400)
        return fig
    df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True)
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = (df.dropna(subset=["time", "open", "high", "low", "close"])
            .drop_duplicates("time").sort_values("time").reset_index(drop=True))
    if df.empty:
        fig = go.Figure()
        fig.update_layout(template="plotly_dark", title="No valid candles to display", height=400)
        return fig

    chart_symbol = config.get("general.symbol", "BTCUSD")
    positions = [p for p in positions if p.get("symbol") == chart_symbol]
    if config.get("execution.mode", "paper") == "delta_demo":
        positions = [p for p in positions
                     if p.get("entry_config", {}).get("delta_entry_order_id")]

    from indicators import apply_all_indicators
    cfg_ind = config.get_section("indicators")
    if "bb_upper" not in df.columns:
        df = apply_all_indicators(df.copy(), cfg_ind)

    fig = go.Figure()

    # Candlesticks
    fig.add_trace(go.Candlestick(
        x=df["time"], open=df["open"], high=df["high"],
        low=df["low"], close=df["close"],
        name="BTC", increasing_line_color="#4caf50",
        decreasing_line_color="#ef5350",
        increasing_fillcolor="#1b5e20", decreasing_fillcolor="#b71c1c"
    ))

    # Bollinger Bands
    if "bb_upper" in df.columns:
        fig.add_trace(go.Scatter(x=df["time"], y=df["bb_upper"], name="BB Upper",
                                 line=dict(color="#42a5f5", width=1, dash="dot"), opacity=0.7))
        fig.add_trace(go.Scatter(x=df["time"], y=df["bb_mid"], name="BB Mid",
                                 line=dict(color="#78909c", width=1), opacity=0.5))
        fig.add_trace(go.Scatter(x=df["time"], y=df["bb_lower"], name="BB Lower",
                                 line=dict(color="#42a5f5", width=1, dash="dot"), opacity=0.7,
                                 fill="tonexty", fillcolor="rgba(66,165,245,0.05)"))

    # EMA
    if "ema" in df.columns:
        fig.add_trace(go.Scatter(x=df["time"], y=df["ema"], name="EMA",
                                 line=dict(color="#ffa726", width=1.5), opacity=0.8))
    if "ema_fast" in df.columns:
        fig.add_trace(go.Scatter(x=df["time"], y=df["ema_fast"], name="Fast EMA",
                                 line=dict(color="#ffca28", width=1.4), opacity=0.9))
    if "ema_slow" in df.columns:
        fig.add_trace(go.Scatter(x=df["time"], y=df["ema_slow"], name="Slow EMA",
                                 line=dict(color="#ab47bc", width=1.4), opacity=0.9))

    # Open positions markers
    for pos in positions:
        color = "#4caf50" if pos["direction"] == "LONG" else "#ef5350"
        symbol_m = "triangle-up" if pos["direction"] == "LONG" else "triangle-down"
        entry_price = float(pos["entry_price"])
        stop_price = float(pos["sl_price"])
        tp_price = float(pos.get("tp_price") or 0)
        fig.add_hline(y=pos["entry_price"], line_dash="dash",
                      line_color=color, opacity=0.6,
                      annotation_text=f"Entry {pos['direction']} ${entry_price:,.2f}", annotation_font_color=color)
        initial_sl = float(pos.get("initial_sl_price", pos["sl_price"]))
        if abs(initial_sl - float(pos["sl_price"])) >= 0.01:
            fig.add_hline(y=initial_sl, line_dash="dash", line_color="#78909c", opacity=0.45,
                          annotation_text=f"Initial SL ${initial_sl:,.2f}", annotation_font_color="#90a4ae")
        if pos.get("sl_enabled", 1):
            fig.add_hline(y=pos["sl_price"], line_dash="dot",
                          line_color="#ef5350", opacity=0.5,
                          annotation_text=f"SL ${stop_price:,.2f}", annotation_font_color="#ef5350")
        if pos.get("tp_enabled", 1) and tp_price > 0:
            fig.add_hline(y=pos["tp_price"], line_dash="dot",
                          line_color="#4caf50", opacity=0.5,
                          annotation_text=f"TP ${tp_price:,.2f}", annotation_font_color="#4caf50")
        entry_time = pd.Timestamp(pos["entry_time"])
        if entry_time.tzinfo is None:
            # Stored engine times are naive IST; candles use UTC timestamps.
            entry_time = entry_time.tz_localize("Asia/Kolkata")
        entry_time = entry_time.tz_convert("UTC")
        fig.add_trace(go.Scatter(x=[entry_time], y=[entry_price], mode="markers",
            name=f"{'BUY' if pos['direction'] == 'LONG' else 'SELL'} #{pos['id']}",
            marker=dict(symbol=symbol_m, size=12, color=color, line=dict(width=1, color="white"))))
    if market_price is not None:
        fig.add_hline(y=float(market_price), line_dash="solid", line_color="#26c6da", opacity=0.9,
                      annotation_text=f"Market ${float(market_price):,.2f}", annotation_font_color="#26c6da")

    timeframe = config.get("data.timeframe", "1m")
    tf_minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
                  "1h": 60, "4h": 240}.get(timeframe, 1)
    range_count = 50 * tf_minutes
    range_step = "minute" if range_count < 60 else "hour"
    range_count = range_count if range_step == "minute" else max(1, round(range_count / 60))

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0a0e1a",
        plot_bgcolor="#0a0e1a",
        title=dict(text=title, font=dict(color="#90caf9", size=14)),
        # Keep a visible overview navigator and leave both axes interactive. The
        # stable uirevision below lets Plotly retain the user's zoom/pan on refresh.
        xaxis=dict(
            gridcolor="#1e2a45", showgrid=True, fixedrange=False,
            rangeslider=dict(visible=True, thickness=0.10, bgcolor="#111827",
                             bordercolor="#263449", borderwidth=1),
            rangeselector=dict(
                buttons=[
                    dict(count=range_count, label="50 candles", step=range_step, stepmode="backward"),
                    dict(count=3, label="3h", step="hour", stepmode="backward"),
                    dict(count=12, label="12h", step="hour", stepmode="backward"),
                    dict(step="all", label="All"),
                ],
                bgcolor="#111827", activecolor="#1d4ed8", font=dict(color="#cbd5e1"),
            ),
        ),
        yaxis=dict(gridcolor="#1e2a45", showgrid=True, side="right", fixedrange=False,
                   autorange=True, automargin=True),
        dragmode="pan",
        # Keep this key stable as new candles arrive, so auto-refresh does not
        # reset a manually selected chart view.
        uirevision=f"live-chart:{title}",
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color="#94a3b8", size=11)),
        margin=dict(l=0, r=0, t=40, b=0),
        height=560
    )
    return fig


# ====================================================================== #
# SIDEBAR
# ====================================================================== #

with st.sidebar:
    st.markdown("## 📊 SMC Algo Terminal")
    st.markdown("---")

    state = get_state()
    eng_status = state.get("engine_status", "stopped")
    heartbeat = state.get("engine_heartbeat", "—")

    st.markdown(f"**Engine:** {status_badge(eng_status)}", unsafe_allow_html=True)
    st.caption(f"Last heartbeat: {heartbeat}")

    price, src = get_live_price()
    symbol = config.get("general.symbol", "BTCUSDT")
    if price:
        st.markdown(f"### 💲 {symbol}")
        st.markdown(f"<h2 style='color:#4caf50;margin:0'>${price:,.2f}</h2>", unsafe_allow_html=True)
        st.caption(f"Source: {src} | {config.get('data.timeframe','1m')} timeframe")
    else:
        st.warning("Price unavailable")

    st.markdown("---")
    st.markdown("**Navigation**")
    page = st.radio("", ["🖥️ Terminal", "⚙️ Settings", "🔬 Monitor"],
                    label_visibility="collapsed")
    st.markdown("---")
    if config.get("execution.mode", "paper") == "delta_demo":
        st.caption("Delta India Demo/Testnet · BTCUSD only · demo credentials required")
    else:
        st.caption("Offline Paper Simulator · no exchange orders")


# ====================================================================== #
# PAGE 1 — TRADING TERMINAL
# ====================================================================== #

if "Terminal" in page:
    # Header
    col_title, col_refresh = st.columns([4, 1])
    with col_title:
        st.markdown("# 🖥️ Trading Terminal")
    with col_refresh:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()

    # ------------------------------------------------------------------ #
    # Metrics row
    # ------------------------------------------------------------------ #
    state = get_state()
    perf = get_performance_summary()
    balance = state.get("balance", config.get("risk.capital", 1000.0))
    if config.get("execution.mode", "paper") == "delta_demo":
        st.info("Delta Demo is the execution mode. Daily loss and profit guards use the Demo wallet baseline plus open-position P&L. Balance and closed-trade cards below are local paper records; exchange position state is shown under Open Positions and Monitor.")
    daily_pnl = state.get("daily_pnl", 0.0)
    daily_trades = state.get("daily_trades", 0)
    total_pnl = perf.get("total_net_pnl") or 0.0
    total_trades = perf.get("total_trades") or 0
    win_rate = perf.get("win_rate") or 0.0
    wins = perf.get("wins") or 0
    losses = perf.get("losses") or 0
    unrealized_net = 0.0
    for pos in get_execution_positions():
        pos_price = get_market_data_client().get_current_price(pos["symbol"])
        qty, entry = pos["quantity"], pos["entry_price"]
        direction = pos["direction"]
        if pos_price is not None:
            gross = (pos_price - entry) * qty if direction == "LONG" else (entry - pos_price) * qty
            fee_rate = float(config.get("execution.fee_rate_per_side", 0.0005))
            unrealized_net += gross - (entry * qty + pos_price * qty) * fee_rate
    equity = balance + unrealized_net

    m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
    m1.metric("💰 Balance", f"${balance:,.2f}",
              delta=f"{'+' if total_pnl >= 0 else ''}{total_pnl:.2f}")
    m2.metric("📈 Equity", f"${equity:,.2f}")
    m3.metric("📅 Daily P&L", f"${daily_pnl:+.2f}")
    m4.metric("📅 Daily Trades", f"{daily_trades}/{config.get('entry.max_daily_trades', 5)}")
    m5.metric("📈 Win Rate", f"{win_rate:.1f}%", delta=f"{wins}W/{losses}L")
    m6.metric("📊 Total Trades", total_trades)
    m7.metric("🏆 Total Net P&L", f"${total_pnl:+.4f}")

    runtime = get_strategy_runtime()
    pending = runtime.get("pending")
    strategy_status_cols = st.columns(3)
    active_strategy_name = config.get("general.strategy_name", "SMC BB Breakout")
    if active_strategy_name == LEGACY_EMA_STRATEGY:
        active_strategy_name = EMA_TRAIL_STRATEGY
    strategy_status_cols[0].metric("Active Strategy", active_strategy_name)
    strategy_status_cols[1].metric("Backend / Paper Engine", state.get("engine_status", "stopped").upper())
    strategy_status_cols[2].metric("Pending Setup", f"{pending.get('direction')} · waiting {pending.get('candles_waited')} candle(s)" if pending else "None")

    st.markdown("---")

    # ------------------------------------------------------------------ #
    # Chart + Open Positions
    # ------------------------------------------------------------------ #
    col_chart, col_pos = st.columns([3, 1])

    with col_chart:
        st.markdown('<div class="section-header">📈 Live Chart</div>', unsafe_allow_html=True)

        @st.fragment(run_every=60)
        def live_chart():
            # More history gives the range navigator real context while keeping
            # the main view readable; Plotly handles this candle count easily.
            df = get_candle_df(500)
            open_pos = get_open_positions()
            live_price, market_source = get_live_price()
            sym = config.get("general.symbol", "BTCUSDT")
            tf = config.get("data.timeframe", "1m")
            client = get_market_data_client()
            if df is None or df.empty:
                reason = client.get_last_error() or "No candles were returned by the market data service."
                st.warning(f"Chart candles unavailable: {reason}")
            elif client.is_stale(max_age_sec=150):
                age = getattr(client, "data_age_sec", None)
                age_text = f"{age:.0f} seconds" if age is not None else "unknown age"
                st.warning(f"Chart data may be stale ({age_text}). Check the market data connection.")
            fig = build_candlestick_chart(df, open_pos, f"{sym} — {tf}", live_price)
            st.plotly_chart(
                fig,
                use_container_width=True,
                config={
                    "displayModeBar": True,
                    "scrollZoom": True,
                    "doubleClick": "reset",
                    "displaylogo": False,
                    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                },
                key="live_candlestick_chart",
            )
            st.caption(f"Source: {market_source} · {tf} candles · drag to move, scroll to zoom, use the bottom slider, or double-click to reset")

        live_chart()

    with col_pos:
        st.markdown('<div class="section-header">📂 Open Positions</div>', unsafe_allow_html=True)

        @st.fragment(run_every=15)
        def open_positions_panel():
            open_pos = get_open_positions()

            if not open_pos:
                st.info("No open positions")
            else:
                for pos in open_pos:
                    price = get_market_data_client().get_current_price(pos["symbol"])
                    direction = pos["direction"]
                    side = "BUY" if direction == "LONG" else "SELL"
                    entry = pos["entry_price"]
                    qty = pos["quantity"]
                    sl = pos["sl_price"]
                    tp = pos["tp_price"]
                    initial_sl = pos.get("initial_sl_price", sl)
                    initial_risk = float(pos.get("initial_risk_points") or abs(entry - initial_sl))
                    current_rr = (((price - entry) if direction == "LONG" else (entry - price)) / initial_risk
                                  if price and initial_risk > 0 else 0.0)
                    locked_profit = max(0.0, sl - entry) if direction == "LONG" else max(0.0, entry - sl)
                    at_breakeven = sl >= entry if direction == "LONG" else sl <= entry
                    trail_status = html.escape(str(pos.get("trailing_status", "active")))
                    color = "#4caf50" if direction == "LONG" else "#ef5350"
                    emoji = "🟢" if direction == "LONG" else "🔴"
                    safe_reason = html.escape(str(pos.get("signal_reason", "")))
                    mirror_status = html.escape(str(pos.get("delta_status", "SKIPPED")))
                    mirror_reason = html.escape(str(pos.get("delta_reason") or ""))

                    unreal = 0.0
                    estimated_fees = 0.0
                    if price:
                        unreal = (price - entry) * qty if direction == "LONG" else (entry - price) * qty
                        fee_rate = float(config.get("execution.fee_rate_per_side", 0.0005))
                        estimated_fees = (entry * qty + price * qty) * fee_rate

                    net_unreal = unreal - estimated_fees
                    unreal_color = "#4caf50" if net_unreal >= 0 else "#ef5350"

                    st.markdown(f"""
                    <div style="background:#111827;border:1px solid {color}33;border-left:4px solid {color};
                    border-radius:8px;padding:12px;margin-bottom:8px">
                        <div style="color:{color};font-weight:700;font-size:15px">{emoji} {side} · Trade #{pos['id']} · {pos['symbol']}</div>
                        <div style="color:#64748b;font-size:11px">Status: OPEN · Entry time: {pos['entry_time']}</div>
                        <div style="color:#64748b;font-size:11px">Protective Stop: SIMULATED · {trail_status}</div>
                        <div style="color:#94a3b8;font-size:12px">Entry: <span style="color:#e2e8f0;font-family:monospace">${entry:,.2f}</span></div>
                        <div style="color:#94a3b8;font-size:12px">Market: <span style="color:#26c6da;font-family:monospace">{'$%0.2f' % price if price else 'Unavailable'}</span></div>
                        <div style="color:#94a3b8;font-size:12px">Qty: <span style="color:#e2e8f0;font-family:monospace">{qty} BTC</span></div>
                        <div style="color:#94a3b8;font-size:12px">Initial SL: <span style="color:#ef5350;font-family:monospace">${initial_sl:,.2f}</span></div>
                        <div style="color:#94a3b8;font-size:12px">Current Active SL: <span style="color:#ef5350;font-family:monospace">${sl:,.2f}</span></div>
                        <div style="color:#94a3b8;font-size:12px">Initial Risk: <span style="color:#e2e8f0;font-family:monospace">{initial_risk:,.2f} points</span> · Current R: <span style="color:#e2e8f0;font-family:monospace">{current_rr:+.2f}R</span></div>
                        <div style="color:#94a3b8;font-size:12px">Trailing: <span style="color:#e2e8f0">{trail_status}</span> · Breakeven: <span style="color:#e2e8f0">{'Yes' if at_breakeven else 'No'}</span> · Locked Profit: <span style="color:#4caf50;font-family:monospace">{locked_profit:,.2f} points</span></div>
                        <div style="color:#94a3b8;font-size:12px">TP: <span style="color:#4caf50;font-family:monospace">{'$%0.2f' % tp if pos.get('tp_enabled', 1) else 'Disabled'}</span></div>
                        <div style="color:#94a3b8;font-size:12px">Delta mirror: <span title="{mirror_reason}">{mirror_status}</span></div>
                        <div style="color:#94a3b8;font-size:12px;margin-top:6px">Unrealized (estimated fees included): <span style="color:{unreal_color};font-weight:600;font-family:monospace">${net_unreal:+.4f}</span></div>
                        <div style="color:#64748b;font-size:11px;margin-top:4px">{safe_reason}</div>
                    </div>
                    """, unsafe_allow_html=True)

        open_positions_panel()

    # ------------------------------------------------------------------ #
    # Closed trades + Activity feed
    # ------------------------------------------------------------------ #
    col_trades, col_feed = st.columns([2, 1])

    with col_trades:
        st.markdown('<div class="section-header">📋 Trade History</div>', unsafe_allow_html=True)

        @st.fragment(run_every=30)
        def trade_history():
            trades = get_all_trades(limit=50)
            if not trades:
                st.info("No completed trades yet.")
                return

            df = pd.DataFrame(trades)
            display_cols = ["id", "exit_time", "symbol", "delta_status", "delta_reason", "direction", "entry_price",
                            "exit_price", "quantity", "net_pnl", "exit_reason"]
            df_display = df[display_cols].copy()
            df_display.columns = ["Trade ID", "Exit Time", "Symbol", "Delta", "Delta Detail", "Dir", "Entry $",
                                   "Exit $", "Qty", "Net P&L", "Reason"]
            df_display["Dir"] = df_display["Dir"].map({"LONG": "BUY", "SHORT": "SELL"}).fillna(df_display["Dir"])
            df_display["Net P&L"] = df_display["Net P&L"].apply(lambda x: f"${x:+.4f}")
            df_display["Entry $"] = df_display["Entry $"].apply(lambda x: f"${x:,.2f}")
            df_display["Exit $"] = df_display["Exit $"].apply(lambda x: f"${x:,.2f}")

            st.dataframe(
                df_display,
                use_container_width=True,
                height=300,
                hide_index=True
            )

            # Export button
            col_e1, col_e2, col_e3 = st.columns([2, 1, 1])
            with col_e2:
                if st.button("📥 Export CSV", use_container_width=True):
                    result = export_trades_csv("data/trades_export.csv")
                    if result:
                        st.success("Exported to data/trades_export.csv")
                    else:
                        st.error("No trades to export")
            with col_e3:
                workbook_path = Path(__file__).parent / "data" / "trade_execution_log.xlsx"
                if workbook_path.exists():
                    st.download_button(
                        "📊 Excel", data=workbook_path.read_bytes(),
                        file_name="trade_execution_log.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                    )
                else:
                    st.button("📊 Excel", disabled=True, use_container_width=True,
                              help="The workbook appears after the first completed trade.")

        trade_history()

    with col_feed:
        st.markdown('<div class="section-header">⚡ Live Activity</div>', unsafe_allow_html=True)

        st.markdown('<div class="section-header">⏳ Pending Signals</div>', unsafe_allow_html=True)
        current_pending = get_strategy_runtime().get("pending")
        if current_pending:
            side = "BUY" if current_pending["direction"] == "LONG" else "SELL"
            st.info(f"{side} setup after crossover at {current_pending['crossover_time']} — waiting {current_pending['candles_waited']} / {config.get('indicators.engulfing_wait_candles', 3)} candles for Engulfing confirmation.")
        else:
            st.caption("No pending EMA crossover setup.")

        @st.fragment(run_every=10)
        def activity_feed():
            events = get_recent_events(limit=20)
            if not events:
                st.info("No activity yet")
                return
            for ev in events:
                lvl = ev.get("level", "INFO")
                css_class = f"event-{lvl.lower()}"
                icon = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "🔴"}.get(lvl, "•")
                ts = ev.get("ts", "")[-8:]
                msg = html.escape(str(ev.get("message", ""))[:120])
                st.markdown(
                    f'<div class="{css_class}">'
                    f'<span style="color:#64748b;font-size:10px">{ts}</span> '
                    f'{icon} {msg}</div>',
                    unsafe_allow_html=True
                )

        activity_feed()

    # ------------------------------------------------------------------ #
    # Performance stats
    # ------------------------------------------------------------------ #
    st.markdown("---")
    st.markdown('<div class="section-header">📊 Performance Summary</div>', unsafe_allow_html=True)

    perf = get_performance_summary()
    pc1, pc2, pc3, pc4 = st.columns(4)
    pc1.metric("Best Trade", f"${(perf.get('best_trade') or 0):+.4f}")
    pc2.metric("Worst Trade", f"${(perf.get('worst_trade') or 0):+.4f}")
    pc3.metric("Avg P&L/Trade", f"${(perf.get('avg_pnl') or 0):+.4f}")
    gross_loss = perf.get("gross_loss") or 0
    pc4.metric("Max Drawdown", f"${gross_loss:.4f}")

    st.markdown('<div class="section-header">🧪 24-Hour Strategy Backtest</div>', unsafe_allow_html=True)
    backtest = get_latest_backtest()
    if not backtest:
        st.info("No 24-hour backtest result is available yet. Start the engine to run one.")
    else:
        metrics = backtest.get("metrics", {})
        bc1, bc2, bc3, bc4 = st.columns(4)
        bc1.metric("Completed Setups", metrics.get("setup_count", 0))
        bc2.metric("Win Rate", f"{metrics.get('win_rate', 0):.1f}%")
        bc3.metric("Profit Factor", f"{metrics.get('profit_factor', 0):.2f}")
        bc4.metric("Max Drawdown", f"{metrics.get('max_drawdown_pct', 0):.2f}%")
        st.caption(
            f"{metrics.get('symbol', '—')} · {metrics.get('candles_analyzed', 0)} candles · "
            f"Run at {backtest.get('run_at', '—')} · Config {str(backtest.get('config_hash') or '')[:12]}"
        )

    # P&L history chart
    trades_hist = get_all_trades(200)
    if trades_hist and len(trades_hist) > 0:
        df_hist = pd.DataFrame(trades_hist)
        df_hist["cumulative_pnl"] = df_hist["net_pnl"].iloc[::-1].cumsum().iloc[::-1].iloc[::-1].cumsum()
        # Recalculate correctly (oldest first)
        df_sorted = df_hist.sort_values("exit_time")
        df_sorted["cumulative_pnl"] = df_sorted["net_pnl"].cumsum()

        fig_pnl = go.Figure()
        colors = ["#4caf50" if v >= 0 else "#ef5350" for v in df_sorted["cumulative_pnl"]]
        fig_pnl.add_trace(go.Scatter(
            x=df_sorted["exit_time"],
            y=df_sorted["cumulative_pnl"],
            mode="lines+markers",
            line=dict(color="#42a5f5", width=2),
            marker=dict(color=colors, size=6),
            fill="tozeroy",
            fillcolor="rgba(66,165,245,0.1)",
            name="Cumulative P&L"
        ))
        fig_pnl.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0a0e1a",
            plot_bgcolor="#0a0e1a",
            height=200,
            margin=dict(l=0, r=0, t=10, b=0),
            xaxis=dict(gridcolor="#1e2a45"),
            yaxis=dict(gridcolor="#1e2a45", side="right")
        )
        st.plotly_chart(fig_pnl, use_container_width=True, config={"displayModeBar": False})


# ====================================================================== #
# PAGE 2 — SETTINGS
# ====================================================================== #

elif "Settings" in page:
    st.markdown("# ⚙️ Strategy Settings")
    if config.get("execution.mode", "paper") == "delta_demo":
        st.caption("Delta India Demo/Testnet · BTCUSD only. Demo orders stay disabled until explicitly enabled below.")
    else:
        st.caption("Paper simulator mode. Settings apply to new trades.")

    cfg = config.get_all()
    changed = False

    with st.form("settings_form"):
        # ---- General ----
        st.markdown('<div class="section-header">🎯 General</div>', unsafe_allow_html=True)
        g1, g2, g3 = st.columns(3)
        with g1:
            strategy_options = ["SMC BB Breakout", LEGACY_EMA_STRATEGY]
            current_strategy = cfg["general"].get("strategy_name", strategy_options[0])
            if current_strategy == EMA_TRAIL_STRATEGY:
                current_strategy = LEGACY_EMA_STRATEGY
            strategy_name = st.selectbox("Active Strategy", strategy_options,
                index=strategy_options.index(current_strategy) if current_strategy in strategy_options else 0,
                format_func=lambda value: EMA_TRAIL_STRATEGY if value == LEGACY_EMA_STRATEGY else value)
        with g2:
            symbol_opts = ["BTCUSD"] if config.get("execution.mode", "paper") == "delta_demo" else ["BTCUSD", "ETHUSD"]
            cur_sym = cfg["general"].get("symbol", "BTCUSD")
            symbol = st.selectbox("Trading Symbol", symbol_opts,
                                  index=symbol_opts.index(cur_sym) if cur_sym in symbol_opts else 0)
        with g3:
            tf_opts = ["1m", "5m", "15m", "1h"]
            cur_tf = cfg["data"].get("timeframe", "1m")
            timeframe = st.selectbox("Candle Timeframe", tf_opts,
                                     index=tf_opts.index(cur_tf) if cur_tf in tf_opts else 0)
        ema_trail_preset = strategy_name in {EMA_TRAIL_STRATEGY, LEGACY_EMA_STRATEGY}
        if ema_trail_preset:
            st.info("Closed-candle EMA crossover entries in both directions · initial SL 100 points · no fixed TP · trailing SL follows each favorable price move by 1 point with a 100-point gap. Updates run on the configured fresh-price polling interval.")
        demo_trading_enabled = bool(cfg.get("execution", {}).get("demo_trading_enabled", False))
        delta_mirror_enabled = st.checkbox("Enable optional Delta Demo mirror",
            value=bool(cfg.get("execution", {}).get("delta_mirror_enabled", False)),
            help="Paper trading always remains independent. Delta failures are shown only as a mirror status.")

        # ---- Risk Management ----
        st.markdown('<div class="section-header">💰 Risk Management</div>', unsafe_allow_html=True)
        r1, r2, r3, r4 = st.columns(4)
        with r1:
            capital = st.number_input("Starting Capital ($)",
                                      min_value=100.0, max_value=1000000.0,
                                      value=float(cfg["risk"].get("capital", 1000.0)),
                                      step=100.0,
                                      help="Simulated account balance")
        with r2:
            risk_pct = st.number_input("Risk per Trade (%)",
                                       min_value=0.1, max_value=10.0,
                                       value=float(cfg["risk"].get("risk_pct", 2.0)),
                                       step=0.1,
                                       help="% of capital risked per trade")
        with r3:
            max_daily_loss = st.number_input("Max Daily Loss (%)",
                                             min_value=1.0, max_value=50.0,
                                             value=float(cfg["risk"].get("max_daily_loss_pct", 5.0)),
                                             step=0.5,
                                             help="Stop trading after this % daily loss")
        with r4:
            max_positions = st.number_input("Max Open Positions",
                                             min_value=1, max_value=10,
                                             value=int(cfg["risk"].get("max_open_positions", 1)),
                                             step=1)
        rr1, rr2, rr3, rr4 = st.columns(4)
        with rr1:
            fixed_lot = st.number_input("Fixed Lot Size", min_value=0.001, max_value=100.0, value=float(cfg["risk"].get("fixed_lot_size", 0.01)), step=0.001, format="%.3f")
        with rr2:
            sizing_options = ["risk_percent", "fixed_lot"]
            sizing_mode = st.selectbox("Position Size Calculation", sizing_options, index=sizing_options.index(cfg["risk"].get("position_size_mode", "risk_percent")))
        with rr3:
            rr_ratio = st.number_input("Risk-to-Reward Ratio", min_value=0.1, max_value=20.0, value=float(cfg["risk"].get("risk_reward_ratio", 3.0)), step=0.1)
        with rr4:
            max_losses = st.number_input("Maximum Consecutive Losses (0 = off)", min_value=0, max_value=100, value=int(cfg["risk"].get("max_consecutive_losses", 0)))
        rr11, rr12 = st.columns(2)
        with rr11:
            min_position_size = st.number_input("Minimum Position Size", min_value=0.0001, max_value=100.0,
                value=float(cfg["risk"].get("minimum_position_size", 0.001)), step=0.001, format="%.4f")
        with rr12:
            max_position_size = st.number_input("Maximum Position Size", min_value=0.0001, max_value=100.0,
                value=float(cfg["risk"].get("maximum_position_size", 1.0)), step=0.01, format="%.4f")
        rr5, rr6, rr7, rr8 = st.columns(4)
        with rr5:
            daily_profit = st.number_input("Daily Profit Target (%) (0 = off)", min_value=0.0, max_value=100.0, value=float(cfg["risk"].get("max_daily_profit_pct", 0.0)), step=0.5)
        with rr6:
            loss_amount_label = ("Maximum Daily Loss (settlement units, 0 = percent limit)"
                if config.get("execution.mode", "paper") == "delta_demo"
                else "Maximum Daily Loss ($, 0 = percent limit)")
            daily_loss_amount = st.number_input(loss_amount_label, min_value=0.0, max_value=1000000.0, value=float(cfg["risk"].get("max_daily_loss_amount", 0.0)), step=10.0)
        with rr7:
            session_options = ["all_day", "custom"]
            session = st.selectbox("Trading Session", session_options, index=session_options.index(cfg["risk"].get("trading_session", "all_day")))
        with rr8:
            direction_options = ["both", "buy_only", "sell_only"]
            trade_direction = st.selectbox("Trade Direction", direction_options, index=direction_options.index(cfg["entry"].get("trade_direction", "both")))
        rr9, rr10 = st.columns(2)
        with rr9:
            trading_start = st.text_input("Trading Start Time (HH:MM)", value=cfg["risk"].get("trading_start_time", "00:00"))
        with rr10:
            trading_end = st.text_input("Trading End Time (HH:MM)", value=cfg["risk"].get("trading_end_time", "23:59"))
        fee1, fee2 = st.columns(2)
        with fee1:
            fee_rate = st.number_input("Simulated Fee per Side (rate)", min_value=0.0, max_value=0.02,
                value=float(cfg.get("execution", {}).get("fee_rate_per_side", 0.0005)), step=0.0001, format="%.4f")
        with fee2:
            slippage_bps = st.number_input("Simulated Slippage (basis points)", min_value=0.0, max_value=500.0,
                value=float(cfg.get("execution", {}).get("slippage_bps", 2.0)), step=0.5)

        # ---- Protective Stop and Trailing Management ----
        st.markdown('<div class="section-header">🛡️ Stop Loss & Trailing Management</div>', unsafe_allow_html=True)
        sx1, sx2, sx3 = st.columns(3)
        with sx1:
            mandatory_sl = st.checkbox("Mandatory Stop Loss",
                value=bool(cfg["exit"].get("mandatory_stop_loss", True)),
                help="Off karne par naye trades block honge; bina protective SL koi entry nahi hogi.")
            if not mandatory_sl:
                st.warning("Mandatory SL off hai; engine naye trades execute nahi karega.")
        with sx2:
            sl_modes = ["fixed_points", "custom"]
            stored_sl_mode = cfg["exit"].get("sl_calculation_mode", "fixed_points")
            sl_calculation_mode = st.selectbox("General Strategy SL Mode", sl_modes,
                index=sl_modes.index(stored_sl_mode) if stored_sl_mode in sl_modes else 0,
                help="Used by non-EMA strategies. EMA trades use the dedicated EMA SL mode below. Fixed Points is 50 points by default; Custom keeps legacy percent/fixed support.")
        with sx3:
            minimum_sl_points = st.number_input("Minimum SL Distance (points)", min_value=0.01,
                max_value=10000.0, value=float(cfg["exit"].get("minimum_sl_points", 0.01)), step=0.01,
                help="Is minimum risk distance se chhota stop ho to trade reject hoga.")
        sx4, sx5, sx6 = st.columns(3)
        with sx4:
            sl_points = st.number_input("Default Stop Loss Points", min_value=0.01,
                max_value=100000.0, value=float(cfg["exit"].get("sl_points", 50.0)), step=1.0,
                help="BTC/ETH USD paper symbols mein ek point = $1 price difference.")
        with sx5:
            custom_sl_mode = st.selectbox("Custom SL Type", ["pct", "fixed"],
                index=0 if cfg["exit"].get("sl_mode", "pct") == "pct" else 1,
                help="Custom compatibility mode: percent of entry or fixed price distance.")
        with sx6:
            sl_val = st.number_input("Custom SL Value", min_value=0.01, max_value=10000.0,
                value=float(cfg["exit"].get("sl_value", 0.5)), step=0.1,
                help="Used only in Custom mode. Percent mode: 0.5 means 0.5%.")

        st.markdown("**Dynamic Trailing Stop**")
        tr1, tr2, tr3, tr4 = st.columns(4)
        with tr1:
            use_trailing = st.checkbox("Enable Dynamic Trailing",
                value=bool(cfg["exit"].get("use_trailing_stop", True)),
                help="Trailing settings are snapshotted when a new trade opens.")
        with tr2:
            trailing_methods = ["rr", "fixed_points", "percentage", "atr"]
            trailing_method = st.selectbox("Trailing Method", trailing_methods,
                index=trailing_methods.index(cfg["exit"].get("trailing_method", "rr")))
        with tr3:
            update_frequencies = ["tick", "candle_close"]
            trailing_frequency = st.selectbox("Update Frequency", update_frequencies,
                index=update_frequencies.index(cfg["exit"].get("trailing_update_frequency", "tick")),
                help="Tick means each fresh ticker poll; Candle Close waits for a completed candle.")
        with tr4:
            fee_adjust_breakeven = st.checkbox("Adjust Breakeven for Fees",
                value=bool(cfg["exit"].get("fee_adjust_breakeven", False)))
        tr5, tr6, tr7, tr8 = st.columns(4)
        with tr5:
            breakeven_trigger = st.number_input("Breakeven Trigger (R)", min_value=0.1,
                max_value=100.0, value=float(cfg["exit"].get("breakeven_trigger_rr", 2.0)), step=0.1)
        with tr6:
            profit_lock_trigger = st.number_input("Profit Lock Trigger (R)", min_value=0.2,
                max_value=100.0, value=float(cfg["exit"].get("profit_lock_trigger_rr", 3.0)), step=0.1)
        with tr7:
            profit_lock_level = st.number_input("Profit Lock Level (R)", min_value=0.1,
                max_value=99.0, value=float(cfg["exit"].get("profit_lock_level_rr", 2.0)), step=0.1)
        with tr8:
            trailing_points = st.number_input("Fixed Trailing Distance (points)", min_value=0.01,
                max_value=100000.0, value=float(cfg["exit"].get("trailing_stop_points", 50.0)), step=1.0)
        tr9, tr10, tr11 = st.columns(3)
        with tr9:
            trailing_pct = st.number_input("Percentage Trail Distance (%)", min_value=0.01,
                max_value=20.0, value=float(cfg["exit"].get("trailing_stop_pct", 0.3)), step=0.05)
        with tr10:
            atr_period = st.number_input("ATR Period", min_value=2, max_value=200,
                value=int(cfg["exit"].get("atr_period", 14)), step=1)
        with tr11:
            atr_multiplier = st.number_input("ATR Multiplier", min_value=0.1, max_value=20.0,
                value=float(cfg["exit"].get("atr_multiplier", 2.0)), step=0.1)

        st.markdown("**Optional Take Profit**")
        tp1, tp2, tp3 = st.columns(3)
        with tp1:
            use_tp = st.checkbox("Enable Fixed Take Profit",
                value=bool(cfg["exit"].get("use_take_profit", False)),
                help="Disabled by default. Stop-loss/trailing still manages the exit.")
        with tp2:
            tp_modes = ["risk_reward", "pct", "fixed"]
            current_tp_mode = cfg["exit"].get("tp_mode", "risk_reward")
            tp_mode = st.selectbox("Take Profit Mode", tp_modes,
                index=tp_modes.index(current_tp_mode) if current_tp_mode in tp_modes else 0)
        with tp3:
            tp_val = st.number_input("Take Profit Value", min_value=0.01, max_value=10000.0,
                value=float(cfg["exit"].get("tp_value", 1.5)), step=0.1)

        # Validate SL < TP in pct mode
        if sl_calculation_mode == "custom" and custom_sl_mode == "pct" and tp_mode == "pct" and sl_val >= tp_val:
            st.warning("⚠️ SL% should be less than TP% for profitable trades")

        # ---- Entry Settings ----
        st.markdown('<div class="section-header">📥 Entry Rules</div>', unsafe_allow_html=True)
        en1, en2, en3 = st.columns(3)
        with en1:
            allow_long = st.checkbox("Allow LONG (Buy)",
                                     value=bool(cfg["entry"].get("allow_long", True)))
            allow_short = st.checkbox("Allow SHORT (Sell)",
                                      value=bool(cfg["entry"].get("allow_short", True)))
        with en2:
            max_daily_trades = st.number_input("Max Daily Trades",
                                               min_value=1, max_value=50,
                                               value=int(cfg["entry"].get("max_daily_trades", 5)),
                                               step=1)
            sr_lookback = st.number_input("S/R Lookback (candles)",
                                          min_value=5, max_value=100,
                                          value=int(cfg["indicators"].get("sr_lookback", 20)),
                                          step=1,
                                          help="How many candles to look back for Support/Resistance")
        with en3:
            rsi_long_max = st.number_input("RSI Max for LONG",
                                            min_value=1, max_value=100,
                                            value=int(cfg["entry"].get("rsi_long_max", 75)),
                                            help="LONG signal ignored if RSI is above this")
            rsi_short_min = st.number_input("RSI Min for SHORT",
                                             min_value=1, max_value=100,
                                             value=int(cfg["entry"].get("rsi_short_min", 25)),
                                             help="SHORT signal ignored if RSI is below this")
        max_engulf_wait = st.number_input("Maximum Candles to Wait for Engulfing Confirmation", min_value=1, max_value=50,
                                          value=int(cfg["indicators"].get("engulfing_wait_candles", 3)))
        signal_modes = ["sequence", "crossover_only", "engulfing_only"]
        ema_signal_mode = st.selectbox("EMA Signal Confirmation Mode", signal_modes,
            index=signal_modes.index(cfg["indicators"].get("ema_signal_mode", "sequence")))
        smc_signal_modes = ["any", "all", "ema_only", "engulfing_only"]
        signal_mode = st.selectbox("SMC Filter Confirmation Mode", smc_signal_modes,
            index=smc_signal_modes.index(cfg["indicators"].get("signal_confirmation_mode", "any")))

        # ---- Indicators ----
        st.markdown('<div class="section-header">📊 Indicators</div>', unsafe_allow_html=True)
        i1, i2, i3 = st.columns(3)
        with i1:
            st.markdown("**Bollinger Bands**")
            bb_period = st.number_input("BB Period", min_value=5, max_value=100,
                                        value=int(cfg["indicators"].get("bb_period", 20)))
            bb_std = st.number_input("BB Std Dev", min_value=0.5, max_value=5.0,
                                     value=float(cfg["indicators"].get("bb_std", 2.0)), step=0.1)
        with i2:
            st.markdown("**RSI Filter**")
            use_rsi = st.checkbox("Enable RSI Filter",
                                  value=bool(cfg["indicators"].get("use_rsi", False)),
                                  help="If ON, RSI thresholds must be satisfied before entry")
            rsi_period = st.number_input("RSI Period", min_value=2, max_value=50,
                                          value=int(cfg["indicators"].get("rsi_period", 14)))
        with i3:
            st.markdown("**EMA Filter**")
            use_ema = st.checkbox("Enable EMA Filter",
                                  value=bool(cfg["indicators"].get("use_ema", False)))
            ema_period = st.number_input("EMA Period", min_value=5, max_value=200,
                                          value=int(cfg["indicators"].get("ema_period", 50)))
            ema_fast = st.number_input("Fast EMA Period", min_value=1, max_value=199, value=int(cfg["indicators"].get("ema_fast_period", 9)))
            ema_slow = st.number_input("Slow EMA Period", min_value=2, max_value=200, value=int(cfg["indicators"].get("ema_slow_period", 15)))
            ema_cross = st.checkbox("Enable EMA Crossover", value=bool(cfg["indicators"].get("ema_crossover_enabled", False)))
            bullish_engulf = st.checkbox("Enable Bullish Engulfing Confirmation", value=bool(cfg["indicators"].get("bullish_engulfing_enabled", False)))
            bearish_engulf = st.checkbox("Enable Bearish Engulfing Confirmation", value=bool(cfg["indicators"].get("bearish_engulfing_enabled", False)))
            ema_sl_modes = ["candle_extreme", "fixed_points", "pct", "fixed"]
            ema_sl_mode = st.selectbox("EMA Strategy Stop Loss Mode", ema_sl_modes,
                index=ema_sl_modes.index(cfg["indicators"].get("ema_sl_mode", "candle_extreme")))
            use_macd = st.checkbox("Enable MACD",
                                   value=bool(cfg["indicators"].get("use_macd", False)))

        # ---- Processing ----
        st.markdown('<div class="section-header">⏱️ Processing Intervals</div>', unsafe_allow_html=True)
        p1, p2 = st.columns(2)
        with p1:
            analysis_interval = st.number_input("Analysis Interval (sec)",
                                                 min_value=10, max_value=3600,
                                                 value=int(cfg["processing"].get("analysis_interval_sec", 60)),
                                                 step=10,
                                                 help="How often to check for new signals")
        with p2:
            pos_check = st.number_input("Position Check Interval (sec)",
                                         min_value=5, max_value=300,
                                         value=int(cfg["processing"].get("position_check_interval_sec", 30)),
                                         step=5,
                                         help="How often to check SL/TP")

        st.markdown("---")
        col_save, col_reset, col_msg = st.columns([1, 1, 3])
        with col_save:
            submitted = st.form_submit_button("💾 SAVE SETTINGS", type="primary", use_container_width=True)
        with col_reset:
            reset_clicked = st.form_submit_button("🔄 Reset Defaults", use_container_width=True)

    if submitted:
        errors = []
        if strategy_name in {EMA_TRAIL_STRATEGY, LEGACY_EMA_STRATEGY}:
            allow_long = True
            allow_short = True
            trade_direction = "both"
            ema_cross = True
            bullish_engulf = False
            bearish_engulf = False
            ema_signal_mode = "crossover_only"
            ema_sl_mode = "fixed_points"
            sl_points = 100.0
            use_tp = False
            use_trailing = True
            trailing_method = "fixed_points"
            trailing_frequency = "tick"
            trailing_points = 100.0
            pos_check = 5
        if use_tp and sl_calculation_mode == "custom" and custom_sl_mode == "pct" and tp_mode == "pct" and sl_val >= tp_val:
            errors.append("SL% must be less than TP%")
        if not allow_long and not allow_short:
            errors.append("At least one of LONG or SHORT must be enabled")
        if ema_slow <= ema_fast:
            errors.append("Slow EMA period must be greater than Fast EMA period")
        if strategy_name in {EMA_TRAIL_STRATEGY, LEGACY_EMA_STRATEGY} and not (ema_cross or bullish_engulf or bearish_engulf):
            errors.append("EMA strategy needs crossover or at least one Engulfing confirmation enabled")
        if (strategy_name in {EMA_TRAIL_STRATEGY, LEGACY_EMA_STRATEGY} and ema_signal_mode == "sequence"
                and ema_cross and not ((allow_long and bullish_engulf) or (allow_short and bearish_engulf))):
            errors.append("Sequence mode needs engulfing confirmation enabled for an allowed direction; choose crossover_only for EMA-only entries")
        if min_position_size > max_position_size:
            errors.append("Minimum position size cannot exceed maximum position size")
        if profit_lock_trigger <= breakeven_trigger:
            errors.append("Profit-lock trigger must be greater than breakeven trigger")
        if profit_lock_level >= profit_lock_trigger:
            errors.append("Profit-lock level must be below the profit-lock trigger")
        try:
            datetime.strptime(trading_start, "%H:%M")
            datetime.strptime(trading_end, "%H:%M")
        except ValueError:
            errors.append("Trading times must use HH:MM format")

        if errors:
            for err in errors:
                st.error(f"❌ {err}")
        else:
            updated = {k: dict(v) for k, v in cfg.items()}
            updated["general"].update(strategy_name=strategy_name, symbol=symbol, sim_mode=True, timezone="Asia/Kolkata")
            updated["data"].update(timeframe=timeframe)
            updated["risk"].update(capital=capital, risk_pct=risk_pct, max_daily_loss_pct=max_daily_loss,
                max_open_positions=max_positions, fixed_lot_size=fixed_lot, position_size_mode=sizing_mode,
                minimum_position_size=min_position_size, maximum_position_size=max_position_size,
                risk_reward_ratio=rr_ratio, max_consecutive_losses=max_losses, max_daily_profit_pct=daily_profit,
                max_daily_loss_amount=daily_loss_amount, trading_session=session,
                trading_start_time=trading_start, trading_end_time=trading_end)
            updated["exit"].update(mandatory_stop_loss=mandatory_sl, minimum_sl_points=minimum_sl_points,
                sl_calculation_mode=sl_calculation_mode, sl_points=sl_points,
                sl_mode=custom_sl_mode, sl_value=sl_val, tp_mode=tp_mode, tp_value=tp_val,
                use_stop_loss=True, use_take_profit=use_tp, risk_reward_ratio=rr_ratio,
                use_trailing_stop=use_trailing, trailing_method=trailing_method,
                trailing_update_frequency=trailing_frequency, breakeven_trigger_rr=breakeven_trigger,
                profit_lock_trigger_rr=profit_lock_trigger, profit_lock_level_rr=profit_lock_level,
                trailing_stop_points=trailing_points, trailing_stop_pct=trailing_pct,
                atr_period=atr_period, atr_multiplier=atr_multiplier,
                fee_adjust_breakeven=fee_adjust_breakeven)
            updated["entry"].update(allow_long=allow_long, allow_short=allow_short, trade_direction=trade_direction,
                max_daily_trades=max_daily_trades, rsi_long_max=rsi_long_max, rsi_short_min=rsi_short_min)
            updated["indicators"].update(bb_period=bb_period, bb_std=bb_std, rsi_period=rsi_period,
                ema_period=ema_period, ema_fast_period=ema_fast, ema_slow_period=ema_slow,
                ema_crossover_enabled=ema_cross, bullish_engulfing_enabled=bullish_engulf,
                bearish_engulfing_enabled=bearish_engulf, signal_confirmation_mode=signal_mode,
                engulfing_wait_candles=max_engulf_wait, sr_lookback=sr_lookback,
                use_rsi=use_rsi, use_ema=use_ema, use_macd=use_macd)
            updated["indicators"]["ema_signal_mode"] = ema_signal_mode
            updated["indicators"]["ema_sl_mode"] = ema_sl_mode
            updated["entry"]["max_daily_trades"] = max_daily_trades
            updated["processing"].update(analysis_interval_sec=analysis_interval, position_check_interval_sec=pos_check)
            updated.setdefault("execution", {}).update(fee_rate_per_side=fee_rate,
                slippage_bps=slippage_bps, demo_trading_enabled=demo_trading_enabled,
                delta_mirror_enabled=delta_mirror_enabled, mode="paper")
            try:
                if not config.update_all(updated):
                    st.error("Settings file save nahi hui. Purani configuration hi active hai.")
                else:
                    from database import log_event
                    log_event("INFO", "dashboard", "Settings persisted; waiting for engine acknowledgement")
                    status_file = Path("data/config_status.json")
                    applied = False
                    if status_file.exists():
                        try:
                            applied = json.loads(status_file.read_text(encoding="utf-8")).get("applied_hash") == config.fingerprint()
                        except (OSError, ValueError):
                            pass
                    if applied:
                        st.success("Settings saved and engine confirmed they are active.")
                    else:
                        st.info("Settings saved. Engine confirmation pending; success tab dikhayega jab engine updated config read karega.")
            except ValueError as exc:
                st.error(f"Settings validation failed: {exc}")

    if reset_clicked:
        try:
            config.update_all(DEFAULTS)
            st.info("Defaults persisted; engine confirmation is pending.")
        except ValueError as exc:
            st.error(f"Could not reset settings: {exc}")

    @st.fragment(run_every=2)
    def config_sync_indicator():
        status_path = Path(__file__).parent / "data" / "config_status.json"
        applied_hash = None
        try:
            applied_hash = json.loads(status_path.read_text(encoding="utf-8")).get("applied_hash")
        except (OSError, ValueError):
            pass
        if applied_hash == config.fingerprint():
            st.success("Backend ne current settings apply karne ki confirmation di hai.")
        elif get_state().get("engine_status") == "running":
            st.info("Settings saved hain; backend acknowledgement ka intezar hai.")
        else:
            st.warning("Settings file mein saved hain; engine band hai, isliye apply confirmation pending hai.")

    config_sync_indicator()


# ====================================================================== #
# PAGE 3 — SYSTEM MONITOR
# ====================================================================== #

elif "Monitor" in page:
    st.markdown("# 🔬 System Monitor")

    # Auto-refresh every 10 seconds
    @st.fragment(run_every=10)
    def monitor_panel():
        state = get_state()
        eng_status = state.get("engine_status", "stopped")
        heartbeat = state.get("engine_heartbeat", "—")
        last_candle = state.get("last_candle_ts", "—")
        last_signal = state.get("last_signal_time", "—")
        last_signal_info = state.get("last_signal_info", "—")

        # Engine status row
        st.markdown('<div class="section-header">🔌 Engine Status</div>', unsafe_allow_html=True)
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(f"**Status:** {status_badge(eng_status)}", unsafe_allow_html=True)
        c2.metric("Heartbeat", heartbeat[-8:] if heartbeat and heartbeat != "—" else "—")
        c3.metric("Last Candle", last_candle[-8:] if last_candle and last_candle != "—" else "—")
        c4.metric("Last Signal", last_signal[-8:] if last_signal and last_signal != "—" else "—")

        execution_mode = config.get("execution.mode", "paper")
        st.markdown('<div class="section-header">🏦 Exchange Execution</div>', unsafe_allow_html=True)
        if execution_mode == "delta_demo":
            exchange = get_exchange_status()
            orders_enabled = bool(config.get("execution.demo_trading_enabled", False))
            checked_at = exchange.get("checked_at")
            display_message = exchange.get("message", "No account check recorded yet")
            if "ip_not_whitelisted_for_api_key" in str(display_message):
                display_message = "This PC's public IP is not on the Demo API key whitelist. Add it in Delta Account → Manage API Keys, then retry verification."
            if exchange.get("connected") and not orders_enabled:
                display_message = ("Automatic orders disabled in Settings; account check recorded at " + str(checked_at)
                    if checked_at else "Orders disabled in Settings; restart the engine for a fresh account verification")
            if exchange.get("ready"):
                st.success(f"Delta India Demo/Testnet · {exchange.get('symbol', 'BTCUSD')} · {display_message}")
            elif exchange.get("connected"):
                st.warning(f"Delta Demo connected; orders disabled · {display_message}")
            else:
                st.error(f"Delta Demo verification failed · {display_message}")
            position = exchange.get("position") or {}
            size = int(position.get("size") or 0)
            if size:
                p1, p2, p3 = st.columns(3)
                p1.metric("Exchange Position", "LONG" if size > 0 else "SHORT")
                p2.metric("Contracts", f"{abs(size)}")
                p3.metric("Entry Price", f"${float(position.get('entry_price') or 0):,.2f}")
            elif exchange.get("connected") and checked_at:
                st.info("Last checked exchange position: FLAT (0 contracts)")
            elif exchange.get("connected"):
                st.warning("Saved position status may be old. Restart the engine to verify the Demo account again.")
            check_label = f"Last account check: {checked_at}" if checked_at else "No fresh verification timestamp recorded"
            st.caption(f"{check_label} · open exchange orders: {len(exchange.get('open_orders') or [])} · API credentials are never displayed here.")
        else:
            st.info("Paper simulator mode · no exchange orders are sent.")

        if last_signal_info and last_signal_info != "—":
            st.info(f"🚦 Last signal at {last_signal}: {last_signal_info}")

        # Data source
        st.markdown('<div class="section-header">📡 Data Source</div>', unsafe_allow_html=True)
        client = get_market_data_client()
        market_symbol = config.get("general.symbol", "BTCUSD")
        market_tf = config.get("data.timeframe", "1m")
        market_candles = client.get_candles(market_symbol, market_tf, limit=3)
        is_fallback = client.is_using_fallback()
        last_err = client.get_last_error()
        if is_fallback:
            st.warning(f"📂 Using CSV fallback data. Delta API error: {last_err}")
        elif market_candles is None or market_candles.empty:
            st.error(f"Delta public market data unavailable: {last_err or 'No candles returned'}")
        else:
            env_label = "Demo/Testnet" if getattr(client, "env", "production") == "testnet" else "Production market data"
            age = getattr(client, "data_age_sec", None)
            age_text = f"last candle {age:.0f}s old" if age is not None else "freshness unknown"
            if client.is_stale(max_age_sec=150):
                st.warning(f"Delta Exchange India — {env_label} feed returned candles, but data is stale ({age_text}).")
            else:
                st.success(f"✅ Delta Exchange India — {env_label} public market data ({age_text})")

        # DB stats
        st.markdown('<div class="section-header">🗄️ Database</div>', unsafe_allow_html=True)
        db_path = Path("data/paper_sim.db")
        d1, d2, d3 = st.columns(3)
        d1.metric("DB File", "paper_sim.db")
        d2.metric("DB Size", f"{db_path.stat().st_size / 1024:.1f} KB" if db_path.exists() else "—")
        d3.metric("Status", "✅ Connected" if db_path.exists() else "❌ Missing")

        # Controls
        st.markdown('<div class="section-header">🎮 Simulator Controls</div>', unsafe_allow_html=True)
        col_c1, col_c2, col_c3, col_c4 = st.columns(4)
        with col_c1:
            if st.button("🔄 Reset Daily Stats", use_container_width=True):
                reset_daily()
                log_event("INFO", "dashboard", "Manual daily reset by user")
                st.success("Daily stats reset!")
        with col_c2:
            if st.button("🗑️ Close All Positions", use_container_width=True,
                         disabled=config.get("execution.mode", "paper") == "delta_demo"):
                clear_all_positions()
                log_event("WARNING", "dashboard", "All positions manually closed by user")
                st.warning("All open positions closed!")
            if config.get("execution.mode", "paper") == "delta_demo":
                st.caption("Simulator controls cannot close Delta Demo positions. Manage exchange orders in Delta.")
        with col_c3:
            new_balance = st.number_input("Set Balance ($)", min_value=100.0,
                                           value=float(state.get("balance", 1000.0)), step=100.0)
        with col_c4:
            if st.button("💰 Update Balance", use_container_width=True):
                update_state(balance=new_balance)
                log_event("INFO", "dashboard", f"Balance manually set to ${new_balance:.2f}")
                st.success(f"Balance set to ${new_balance:.2f}")

        # Recent signals
        st.markdown('<div class="section-header">🚦 Recent Signals</div>', unsafe_allow_html=True)
        signals = get_recent_signals(20)
        if signals:
            sig_df = pd.DataFrame(signals)[["ts", "signal", "price", "reason", "acted_upon"]]
            sig_df.columns = ["Time", "Signal", "Price $", "Reason", "Traded"]
            sig_df["Signal"] = sig_df["Signal"].map({"LONG": "BUY", "SHORT": "SELL"}).fillna(sig_df["Signal"])
            sig_df["Price $"] = sig_df["Price $"].apply(lambda x: f"${x:,.2f}")
            sig_df["Traded"] = sig_df["Traded"].apply(lambda x: "✅ Yes" if x else "⬜ No")
            st.dataframe(sig_df, use_container_width=True, height=200, hide_index=True)
        else:
            st.info("No signals recorded yet. Start the engine to begin.")

        # Event log
        st.markdown('<div class="section-header">📋 System Log (last 50 events)</div>', unsafe_allow_html=True)
        level_filter = st.selectbox("Filter by level", ["ALL", "INFO", "WARNING", "ERROR"])
        events = get_recent_events(50, level_filter if level_filter != "ALL" else None)
        if events:
            for ev in events:
                lvl = ev.get("level", "INFO")
                css = f"event-{lvl.lower()}"
                icon = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "🔴"}.get(lvl, "•")
                ev_time = html.escape(str(ev.get("ts", "")))
                ev_source = html.escape(str(ev.get("source", "")))
                ev_message = html.escape(str(ev.get("message", "")))
                st.markdown(
                    f'<div class="{css}"><code>{ev_time}</code> '
                    f'{icon} [{ev_source}] {ev_message}</div>',
                    unsafe_allow_html=True
                )
        else:
            st.info("No events logged yet.")

    monitor_panel()
