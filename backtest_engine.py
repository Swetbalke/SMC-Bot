"""Automated 24-hour backtests with persisted metrics and local WebSocket updates."""

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pandas as pd

from backtest import run_ema_backtest
from config_manager import config, EMA_STRATEGY_NAMES
from database import record_backtest_result
from logger_setup import get_logger

logger = get_logger("backtest_engine")
WS_HOST = "127.0.0.1"
WS_PORT = 8765

try:
    import websockets
except ImportError:
    websockets = None


class BacktestEngine:
    """Run once on startup and again when the active strategy config changes."""

    def __init__(self, data_client):
        self._data_client = data_client
        self._running = threading.Event()
        self._stop_event = threading.Event()
        self._trigger_thread = None
        self._ws_thread = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._ws_clients = set()
        self._ws_lock = threading.Lock()
        self._last_config_hash: Optional[str] = None
        self._last_result: Optional[Dict[str, Any]] = None

    def start(self) -> None:
        if self._running.is_set():
            return
        self._running.set()
        self._stop_event.clear()
        self._trigger_thread = threading.Thread(
            target=self._trigger_loop, daemon=True, name="backtest-trigger")
        self._trigger_thread.start()
        if websockets is not None:
            self._ws_thread = threading.Thread(
                target=self._run_ws_server, daemon=True, name="backtest-ws")
            self._ws_thread.start()
        else:
            logger.warning("Backtest WebSocket disabled: install websockets to enable it")

    def stop(self) -> None:
        self._running.clear()
        self._stop_event.set()
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._trigger_thread:
            self._trigger_thread.join(timeout=2)
        if self._ws_thread:
            self._ws_thread.join(timeout=2)

    def _trigger_loop(self) -> None:
        while self._running.is_set():
            try:
                cfg_hash = config.fingerprint()
                if cfg_hash != self._last_config_hash:
                    self._last_config_hash = cfg_hash
                    self._run_backtest(cfg_hash)
            except Exception:
                logger.exception("24-hour backtest failed")
            self._stop_event.wait(timeout=30)

    def _run_backtest(self, cfg_hash: str) -> Optional[Dict[str, Any]]:
        settings = config.get_all()
        strategy_name = settings.get("general", {}).get("strategy_name", "")
        if strategy_name not in EMA_STRATEGY_NAMES:
            logger.info("24-hour backtest skipped: strategy %r is not supported by the existing EMA backtester", strategy_name)
            return None
        symbol = settings.get("general", {}).get("symbol", "BTCUSD")
        candles = self._data_client.get_candles(symbol, "1m", limit=1500)
        if candles is None or candles.empty:
            logger.warning("24-hour backtest skipped: market candles are unavailable")
            return None

        df = candles.copy()
        if getattr(self._data_client, "last_candle_forming", False):
            # Historical evaluation must not treat a still-forming market bar as
            # a completed bar.  The live engine follows the same rule.
            df = df.iloc[:-1].copy()
        df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        df = df[df["time"] >= cutoff].dropna(subset=["time"]).reset_index(drop=True)
        if len(df) < 50:
            logger.warning("24-hour backtest skipped: only %s recent candles", len(df))
            return None

        result = run_ema_backtest(df, settings)
        metrics = {
            "symbol": symbol,
            "setup_count": int(result.get("total_trades", 0)),
            "win_rate": float(result.get("win_rate_pct", 0.0)),
            "profit_factor": float(result.get("profit_factor") or 0.0),
            "max_drawdown_pct": float(result.get("maximum_drawdown_pct", 0.0)),
            "total_pnl": float(result.get("net_profit_loss", 0.0)),
            "candles_analyzed": int(len(df)),
            "window_hours": 24,
            "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "config_hash": cfg_hash,
            "funding_model": result.get("funding_model", "not_modelled"),
        }
        run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        record_backtest_result(run_at, cfg_hash, metrics)
        self._last_result = metrics
        self._broadcast(metrics)
        logger.info("24-hour backtest finished: %s trades, win rate %.1f%%",
                    metrics["setup_count"], metrics["win_rate"])
        return metrics

    def _run_ws_server(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except asyncio.CancelledError:
            pass
        except OSError as exc:
            logger.warning("Backtest WebSocket unavailable on %s:%s: %s", WS_HOST, WS_PORT, exc)
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    async def _serve(self) -> None:
        self._server = await websockets.serve(self._handle_client, WS_HOST, WS_PORT)
        if not self._running.is_set():
            self._server.close()
            return
        await self._server.serve_forever()

    async def _handle_client(self, websocket, *args) -> None:
        with self._ws_lock:
            self._ws_clients.add(websocket)
        try:
            if self._last_result:
                await websocket.send(json.dumps({"type": "backtest_result", "data": self._last_result}))
            await websocket.wait_closed()
        finally:
            with self._ws_lock:
                self._ws_clients.discard(websocket)

    def _broadcast(self, result: Dict[str, Any]) -> None:
        if not self._loop or not self._ws_clients:
            return
        message = json.dumps({"type": "backtest_result", "data": result})

        async def send_all():
            with self._ws_lock:
                clients = list(self._ws_clients)
            for client in clients:
                try:
                    await client.send(message)
                except Exception:
                    with self._ws_lock:
                        self._ws_clients.discard(client)

        asyncio.run_coroutine_threadsafe(send_all(), self._loop)
