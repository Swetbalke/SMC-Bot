"""
database.py — SQLite-based Persistent State for Paper Simulator
Restart karne par bhi sab kuch yaad rahega.
"""

import sqlite3
import threading
import json
import math
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).parent / "data" / "paper_sim.db"
DB_PATH.parent.mkdir(exist_ok=True)
TRADING_TIMEZONE = ZoneInfo("Asia/Kolkata")

_lock = threading.Lock()


@contextmanager
def _get_conn():
    """WAL-mode connection that commits/rolls back and always closes."""
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables if they don't exist."""
    with _lock, _get_conn() as conn:
        # journal_mode is persistent for this database; set it during schema
        # initialization rather than attempting to change it on every reader.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sim_state (
                id              INTEGER PRIMARY KEY CHECK (id = 1),
                balance         REAL    NOT NULL DEFAULT 1000.0,
                daily_pnl       REAL    NOT NULL DEFAULT 0.0,
                daily_trades    INTEGER NOT NULL DEFAULT 0,
                last_reset_date TEXT    NOT NULL DEFAULT '',
                last_candle_ts  TEXT,
                engine_status   TEXT    NOT NULL DEFAULT 'stopped',
                engine_heartbeat TEXT,
                last_signal_time TEXT,
                last_signal_info TEXT,
                exchange_status_json TEXT NOT NULL DEFAULT '{}'
            );

            INSERT OR IGNORE INTO sim_state (id) VALUES (1);

            CREATE TABLE IF NOT EXISTS open_positions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT    NOT NULL,
                direction   TEXT    NOT NULL CHECK (direction IN ('LONG','SHORT')),
                entry_price REAL    NOT NULL,
                quantity    REAL    NOT NULL,
                sl_price    REAL    NOT NULL,
                tp_price    REAL    NOT NULL,
                entry_time  TEXT    NOT NULL,
                signal_reason TEXT,
                sl_enabled INTEGER NOT NULL DEFAULT 1,
                tp_enabled INTEGER NOT NULL DEFAULT 1,
                signal_id TEXT,
                trailing_enabled INTEGER NOT NULL DEFAULT 0,
                rr_milestones_enabled INTEGER NOT NULL DEFAULT 1,
                trailing_stop_pct REAL NOT NULL DEFAULT 0.3,
                initial_sl_price REAL NOT NULL DEFAULT 0,
                initial_risk_points REAL NOT NULL DEFAULT 0,
                trailing_method TEXT NOT NULL DEFAULT 'rr',
                trailing_update_frequency TEXT NOT NULL DEFAULT 'tick',
                breakeven_trigger_rr REAL NOT NULL DEFAULT 2,
                profit_lock_trigger_rr REAL NOT NULL DEFAULT 3,
                profit_lock_level_rr REAL NOT NULL DEFAULT 2,
                trailing_stop_points REAL NOT NULL DEFAULT 50,
                atr_period INTEGER NOT NULL DEFAULT 14,
                atr_multiplier REAL NOT NULL DEFAULT 2,
                fee_adjust_breakeven INTEGER NOT NULL DEFAULT 0,
                best_favorable_price REAL NOT NULL DEFAULT 0,
                trailing_milestone REAL NOT NULL DEFAULT 0,
                trailing_status TEXT NOT NULL DEFAULT 'active',
                trailing_last_candle_time TEXT,
                entry_config_json TEXT NOT NULL DEFAULT '{}'
                ,delta_status TEXT NOT NULL DEFAULT 'SKIPPED'
                ,delta_reason TEXT
                ,delta_order_id TEXT
                ,delta_updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS closed_trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT    NOT NULL,
                direction   TEXT    NOT NULL,
                entry_price REAL    NOT NULL,
                exit_price  REAL    NOT NULL,
                quantity    REAL    NOT NULL,
                sl_price    REAL    NOT NULL,
                tp_price    REAL    NOT NULL,
                gross_pnl   REAL    NOT NULL,
                fee         REAL    NOT NULL DEFAULT 0.0,
                net_pnl     REAL    NOT NULL,
                entry_time  TEXT    NOT NULL,
                exit_time   TEXT    NOT NULL,
                duration_min INTEGER,
                exit_reason TEXT    NOT NULL,
                signal_reason TEXT,
                signal_id TEXT,
                execution_venue TEXT NOT NULL DEFAULT 'PAPER_ONLY',
                delta_order_id TEXT,
                target_rrr REAL NOT NULL DEFAULT 0
                ,delta_status TEXT NOT NULL DEFAULT 'SKIPPED'
                ,delta_reason TEXT
                ,delta_updated_at TEXT
                ,delta_close_status TEXT
                ,delta_close_reason TEXT
                ,delta_close_order_id TEXT
                ,delta_close_updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS strategy_signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                symbol      TEXT    NOT NULL,
                signal      TEXT    NOT NULL,
                price       REAL    NOT NULL,
                reason      TEXT,
                acted_upon  INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS system_events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT    NOT NULL,
                level   TEXT    NOT NULL,
                source  TEXT,
                message TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS strategy_runtime (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                strategy_name TEXT NOT NULL DEFAULT '',
                pending_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT
            );
            INSERT OR IGNORE INTO strategy_runtime (id) VALUES (1);

            CREATE TABLE IF NOT EXISTS backtest_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL,
                window_hours INTEGER NOT NULL DEFAULT 24,
                setup_count INTEGER NOT NULL DEFAULT 0,
                win_rate REAL NOT NULL DEFAULT 0,
                profit_factor REAL NOT NULL DEFAULT 0,
                max_drawdown_pct REAL NOT NULL DEFAULT 0,
                total_pnl REAL NOT NULL DEFAULT 0,
                config_hash TEXT,
                result_json TEXT NOT NULL DEFAULT '{}'
            );
        """)
        # Keep the common dashboard/history scans efficient while WAL readers
        # continue alongside the engine's serialized writes.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_ts ON strategy_signals(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON strategy_signals(symbol, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON closed_trades(exit_time)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol_exit ON closed_trades(symbol, exit_time)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts_level ON system_events(ts, level)")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(open_positions)")}
        state_columns = {row[1] for row in conn.execute("PRAGMA table_info(sim_state)")}
        if "exchange_status_json" not in state_columns:
            conn.execute("ALTER TABLE sim_state ADD COLUMN exchange_status_json TEXT NOT NULL DEFAULT '{}'")
        additions = {
            "sl_enabled": "INTEGER NOT NULL DEFAULT 1",
            "tp_enabled": "INTEGER NOT NULL DEFAULT 1",
            "signal_id": "TEXT",
            "trailing_enabled": "INTEGER NOT NULL DEFAULT 0",
            # Added to existing positions as disabled so their old trailing
            # behavior does not silently acquire new RR milestones.
            "rr_milestones_enabled": "INTEGER NOT NULL DEFAULT 0",
            "trailing_stop_pct": "REAL NOT NULL DEFAULT 0.3",
            "initial_sl_price": "REAL NOT NULL DEFAULT 0",
            "initial_risk_points": "REAL NOT NULL DEFAULT 0",
            "trailing_method": "TEXT NOT NULL DEFAULT 'rr'",
            "trailing_update_frequency": "TEXT NOT NULL DEFAULT 'tick'",
            "breakeven_trigger_rr": "REAL NOT NULL DEFAULT 2",
            "profit_lock_trigger_rr": "REAL NOT NULL DEFAULT 3",
            "profit_lock_level_rr": "REAL NOT NULL DEFAULT 2",
            "trailing_stop_points": "REAL NOT NULL DEFAULT 50",
            "atr_period": "INTEGER NOT NULL DEFAULT 14",
            "atr_multiplier": "REAL NOT NULL DEFAULT 2",
            "fee_adjust_breakeven": "INTEGER NOT NULL DEFAULT 0",
            "best_favorable_price": "REAL NOT NULL DEFAULT 0",
            "trailing_milestone": "REAL NOT NULL DEFAULT 0",
            "trailing_status": "TEXT NOT NULL DEFAULT 'active'",
            "trailing_last_candle_time": "TEXT",
            "entry_config_json": "TEXT NOT NULL DEFAULT '{}'",
            "delta_status": "TEXT NOT NULL DEFAULT 'SKIPPED'",
            "delta_reason": "TEXT",
            "delta_order_id": "TEXT",
            "delta_updated_at": "TEXT",
        }
        closed_columns = {row[1] for row in conn.execute("PRAGMA table_info(closed_trades)")}
        closed_additions = {
            "signal_id": "TEXT",
            "execution_venue": "TEXT NOT NULL DEFAULT 'PAPER_ONLY'",
            "delta_order_id": "TEXT",
            "target_rrr": "REAL NOT NULL DEFAULT 0",
            "delta_status": "TEXT NOT NULL DEFAULT 'SKIPPED'",
            "delta_reason": "TEXT",
            "delta_updated_at": "TEXT",
            "delta_close_status": "TEXT",
            "delta_close_reason": "TEXT",
            "delta_close_order_id": "TEXT",
            "delta_close_updated_at": "TEXT",
        }
        if any(name not in columns for name in additions) or any(name not in closed_columns for name in closed_additions):
            backup_dir = DB_PATH.parent / "backups"
            backup_dir.mkdir(exist_ok=True)
            backup_path = backup_dir / f"paper_sim_pre_migration_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            with sqlite3.connect(str(backup_path)) as backup:
                conn.backup(backup)
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE open_positions ADD COLUMN {name} {declaration}")
        conn.execute("UPDATE open_positions SET initial_sl_price=sl_price WHERE initial_sl_price=0")
        conn.execute("UPDATE open_positions SET initial_risk_points=ABS(entry_price-sl_price) WHERE initial_risk_points=0")
        conn.execute("UPDATE open_positions SET sl_enabled=1 WHERE sl_enabled=0")
        conn.execute("UPDATE open_positions SET best_favorable_price=entry_price WHERE best_favorable_price=0")
        conn.execute("UPDATE open_positions SET trailing_status='disabled' WHERE trailing_enabled=0 AND trailing_status='active'")
        # Legacy open positions used percentage trailing; keep that behavior.
        conn.execute("UPDATE open_positions SET trailing_method='percentage' WHERE trailing_enabled=1 AND entry_config_json='{}' AND trailing_method='rr'")
        for name, declaration in closed_additions.items():
            if name not in closed_columns:
                conn.execute(f"ALTER TABLE closed_trades ADD COLUMN {name} {declaration}")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_open_signal_id ON open_positions(signal_id) WHERE signal_id IS NOT NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_closed_signal_id ON closed_trades(signal_id) WHERE signal_id IS NOT NULL")


# ------------------------------------------------------------------ #
# sim_state helpers
# ------------------------------------------------------------------ #

def get_state() -> Dict[str, Any]:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM sim_state WHERE id=1").fetchone()
        return dict(row) if row else {}


def update_state(**kwargs):
    if not kwargs:
        return
    with _lock, _get_conn() as conn:
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values())
        conn.execute(f"UPDATE sim_state SET {sets} WHERE id=1", vals)


def update_exchange_status(status: Dict[str, Any]):
    """Persist only non-secret exchange status for the dashboard."""
    encoded = json.dumps(status or {}, separators=(",", ":"))
    with _lock, _get_conn() as conn:
        conn.execute("UPDATE sim_state SET exchange_status_json=? WHERE id=1", (encoded,))


def get_exchange_status() -> Dict[str, Any]:
    state = get_state()
    try:
        value = json.loads(state.get("exchange_status_json") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def update_strategy_runtime(strategy_name: str, pending: dict):
    with _lock, _get_conn() as conn:
        conn.execute("UPDATE strategy_runtime SET strategy_name=?, pending_json=?, updated_at=? WHERE id=1",
                     (strategy_name, json.dumps(pending or {}), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def get_strategy_runtime() -> Dict[str, Any]:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM strategy_runtime WHERE id=1").fetchone()
        if not row:
            return {"strategy_name": "", "pending": None, "updated_at": None}
        out = dict(row)
        try:
            out["pending"] = json.loads(out.pop("pending_json") or "{}") or None
        except (TypeError, ValueError):
            out["pending"] = None
        return out


def reset_daily():
    """Daily reset at midnight."""
    # Risk limits and the engine session use IST.  Do not depend on the host
    # timezone, which is commonly UTC on an Ubuntu server.
    today = datetime.now(TRADING_TIMEZONE).date().isoformat()
    update_state(daily_pnl=0.0, daily_trades=0, last_reset_date=today)


def check_and_reset_daily():
    """Auto-reset if date changed since last reset."""
    state = get_state()
    today = datetime.now(TRADING_TIMEZONE).date().isoformat()
    if state.get("last_reset_date", "") != today:
        reset_daily()
        log_event("INFO", "database", f"Daily reset performed for {today}")


# ------------------------------------------------------------------ #
# Open positions
# ------------------------------------------------------------------ #

def save_position(symbol: str, direction: str, entry_price: float,
                  quantity: float, sl_price: float, tp_price: float,
                  entry_time: str, signal_reason: str = "",
                  sl_enabled: bool = True, tp_enabled: bool = True,
                  signal_id: str = None, trailing_enabled: bool = False,
                  trailing_stop_pct: float = 0.3, initial_sl_price: float = None,
                  initial_risk_points: float = None, trailing_settings: Dict = None,
                  entry_config: Dict = None, rr_milestones_enabled: bool = True,
                  delta_status: str = "SKIPPED", delta_reason: str = None,
                  delta_order_id: str = None, delta_updated_at: str = None) -> int:
    trailing_settings = trailing_settings or {}
    initial_sl_price = float(sl_price if initial_sl_price is None else initial_sl_price)
    initial_risk_points = float(abs(entry_price - initial_sl_price) if initial_risk_points is None else initial_risk_points)
    with _lock, _get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO open_positions
            (symbol, direction, entry_price, quantity, sl_price, tp_price, entry_time, signal_reason,
             sl_enabled, tp_enabled, signal_id, trailing_enabled, trailing_stop_pct,
             initial_sl_price, initial_risk_points, rr_milestones_enabled, trailing_method, trailing_update_frequency,
             breakeven_trigger_rr, profit_lock_trigger_rr, profit_lock_level_rr,
             trailing_stop_points, atr_period, atr_multiplier, fee_adjust_breakeven,
             best_favorable_price, trailing_status, entry_config_json,
             delta_status, delta_reason, delta_order_id, delta_updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (symbol, direction, entry_price, quantity, sl_price, tp_price, entry_time, signal_reason,
              int(sl_enabled), int(tp_enabled), signal_id, int(trailing_enabled), trailing_stop_pct,
              initial_sl_price, initial_risk_points, int(rr_milestones_enabled),
              trailing_settings.get("trailing_method", "rr"),
              trailing_settings.get("trailing_update_frequency", "tick"),
              trailing_settings.get("breakeven_trigger_rr", 2.0),
              trailing_settings.get("profit_lock_trigger_rr", 3.0),
              trailing_settings.get("profit_lock_level_rr", 2.0),
              trailing_settings.get("trailing_stop_points", 50.0),
              trailing_settings.get("atr_period", 14), trailing_settings.get("atr_multiplier", 2.0),
              int(trailing_settings.get("fee_adjust_breakeven", False)),
              float(entry_price), "active" if trailing_enabled else "disabled",
              json.dumps(entry_config or {}, sort_keys=True), delta_status, delta_reason,
              delta_order_id, delta_updated_at))
        return cur.lastrowid


def update_open_delta_mirror(pos_id: int, status: str, reason: str = None,
                             order_id: str = None, updated_at: str = None) -> bool:
    """Persist non-sensitive Delta mirror state without affecting paper trade state."""
    with _lock, _get_conn() as conn:
        cur = conn.execute("""UPDATE open_positions
            SET delta_status=?, delta_reason=?, delta_order_id=COALESCE(?, delta_order_id),
                delta_updated_at=? WHERE id=?""",
            (str(status), reason, order_id, updated_at, pos_id))
        return cur.rowcount == 1


def update_closed_delta_mirror(trade_id: int, status: str = None, reason: str = None,
                               order_id: str = None, updated_at: str = None,
                               close_status: str = None, close_reason: str = None,
                               close_order_id: str = None, close_updated_at: str = None) -> bool:
    with _lock, _get_conn() as conn:
        cur = conn.execute("""UPDATE closed_trades SET
            delta_status=COALESCE(?, delta_status), delta_reason=COALESCE(?, delta_reason),
            delta_order_id=COALESCE(?, delta_order_id), delta_updated_at=COALESCE(?, delta_updated_at),
            delta_close_status=COALESCE(?, delta_close_status), delta_close_reason=COALESCE(?, delta_close_reason),
            delta_close_order_id=COALESCE(?, delta_close_order_id),
            delta_close_updated_at=COALESCE(?, delta_close_updated_at) WHERE id=?""",
            (status, reason, order_id, updated_at, close_status, close_reason,
             close_order_id, close_updated_at, trade_id))
        return cur.rowcount == 1


def has_signal_been_traded(signal_id: str) -> bool:
    if not signal_id:
        return False
    with _get_conn() as conn:
        return bool(conn.execute("SELECT 1 FROM open_positions WHERE signal_id=? UNION SELECT 1 FROM closed_trades WHERE signal_id=? LIMIT 1",
                                 (signal_id, signal_id)).fetchone())


def update_position_sl(pos_id: int, sl_price: float):
    row = next((p for p in get_open_positions() if p["id"] == pos_id), None)
    if row is None:
        return False
    result = update_position_stop(pos_id, sl_price,
                                  row.get("best_favorable_price", row["entry_price"]),
                                  row.get("trailing_milestone", 0),
                                  row.get("trailing_status", "active"))
    return result


def update_position_stop(pos_id: int, sl_price: float, best_price: float,
                         milestone: float, status: str) -> bool:
    """Persist a simulated stop update only if it does not loosen protection."""
    with _lock, _get_conn() as conn:
        row = conn.execute("SELECT direction, sl_price FROM open_positions WHERE id=?", (pos_id,)).fetchone()
        if row is None:
            return False
        old = float(row["sl_price"])
        new = float(sl_price)
        if not math.isfinite(new) or new <= 0 or not math.isfinite(float(best_price)):
            return False
        if row["direction"] == "LONG" and new < old - 1e-9:
            return False
        if row["direction"] == "SHORT" and new > old + 1e-9:
            return False
        cur = conn.execute("""UPDATE open_positions SET sl_price=?, best_favorable_price=?,
                          trailing_milestone=?, trailing_status=? WHERE id=? AND sl_price=?""",
                           (new, float(best_price), float(milestone), str(status), pos_id, old))
        return cur.rowcount == 1


def update_trailing_candle_time(pos_id: int, candle_time: str) -> bool:
    with _lock, _get_conn() as conn:
        cur = conn.execute("UPDATE open_positions SET trailing_last_candle_time=? WHERE id=? AND trailing_last_candle_time IS NOT ?",
                           (str(candle_time), pos_id, str(candle_time)))
        return cur.rowcount == 1


def get_open_positions() -> List[Dict]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM open_positions ORDER BY entry_time").fetchall()
        positions = [dict(r) for r in rows]
        for position in positions:
            try:
                position["entry_config"] = json.loads(position.get("entry_config_json") or "{}")
            except (TypeError, ValueError):
                position["entry_config"] = {}
        return positions


def close_position(pos_id: int):
    with _lock, _get_conn() as conn:
        conn.execute("DELETE FROM open_positions WHERE id=?", (pos_id,))


def clear_all_positions():
    with _lock, _get_conn() as conn:
        conn.execute("DELETE FROM open_positions")


# ------------------------------------------------------------------ #
# Closed trades
# ------------------------------------------------------------------ #

def record_trade(symbol: str, direction: str,
                 entry_price: float, exit_price: float, quantity: float,
                 sl_price: float, tp_price: float,
                 gross_pnl: float, fee: float, net_pnl: float,
                 entry_time: str, exit_time: str, duration_min: int,
                 exit_reason: str, signal_reason: str = "", signal_id: str = None,
                 execution_venue: str = "PAPER_ONLY", delta_order_id: str = None,
                 target_rrr: float = 0.0, delta_status: str = "SKIPPED",
                 delta_reason: str = None, delta_updated_at: str = None):
    with _lock, _get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO closed_trades
            (symbol, direction, entry_price, exit_price, quantity,
             sl_price, tp_price, gross_pnl, fee, net_pnl,
             entry_time, exit_time, duration_min, exit_reason, signal_reason, signal_id,
             execution_venue, delta_order_id, target_rrr, delta_status, delta_reason, delta_updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (symbol, direction, entry_price, exit_price, quantity,
              sl_price, tp_price, gross_pnl, fee, net_pnl,
              entry_time, exit_time, duration_min, exit_reason, signal_reason, signal_id,
              execution_venue, delta_order_id, target_rrr, delta_status, delta_reason, delta_updated_at))
        trade_id = cur.lastrowid
        state = conn.execute("SELECT daily_pnl, daily_trades, balance FROM sim_state WHERE id=1").fetchone()
        if state:
            conn.execute("""UPDATE sim_state SET daily_pnl=?, daily_trades=?, balance=? WHERE id=1""",
                         (round(float(state["daily_pnl"]) + net_pnl, 4),
                          int(state["daily_trades"]) + 1,
                          round(float(state["balance"]) + net_pnl, 4)))
    return trade_id


def record_backtest_result(run_at: str, config_hash: str, metrics: Dict[str, Any]) -> int:
    result_json = json.dumps(metrics, separators=(",", ":"), allow_nan=False)
    with _lock, _get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO backtest_results
            (run_at, window_hours, setup_count, win_rate, profit_factor,
             max_drawdown_pct, total_pnl, config_hash, result_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (run_at, int(metrics.get("window_hours", 24)), metrics.get("setup_count", 0),
              metrics.get("win_rate", 0), metrics.get("profit_factor", 0),
              metrics.get("max_drawdown_pct", 0), metrics.get("total_pnl", 0),
              config_hash, result_json))
        return cur.lastrowid


def get_latest_backtest() -> Optional[Dict[str, Any]]:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM backtest_results ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        result["metrics"] = json.loads(result.get("result_json") or "{}")
    except (TypeError, ValueError):
        result["metrics"] = {}
    return result


def get_all_trades(limit: int = 200) -> List[Dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM closed_trades ORDER BY exit_time DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_performance_summary() -> Dict[str, Any]:
    with _get_conn() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN net_pnl <= 0 THEN 1 ELSE 0 END) as losses,
                SUM(net_pnl) as total_net_pnl,
                SUM(CASE WHEN net_pnl > 0 THEN net_pnl ELSE 0 END) as gross_profit,
                SUM(CASE WHEN net_pnl < 0 THEN net_pnl ELSE 0 END) as gross_loss,
                MIN(net_pnl) as worst_trade,
                MAX(net_pnl) as best_trade,
                AVG(net_pnl) as avg_pnl
            FROM closed_trades
        """).fetchone()
        d = dict(row) if row else {}
        total = d.get("total_trades", 0) or 0
        wins = d.get("wins", 0) or 0
        d["win_rate"] = round((wins / total * 100), 1) if total > 0 else 0.0
        return d


def export_trades_csv(filepath: str = "data/trades_export.csv"):
    import csv
    trades = get_all_trades(limit=10000)
    if not trades:
        return False
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=trades[0].keys())
        writer.writeheader()
        writer.writerows(trades)
    return True


# ------------------------------------------------------------------ #
# Strategy signals
# ------------------------------------------------------------------ #

def log_signal(symbol: str, signal: str, price: float,
               reason: str = "", acted_upon: bool = False):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock, _get_conn() as conn:
        conn.execute("""
            INSERT INTO strategy_signals (ts, symbol, signal, price, reason, acted_upon)
            VALUES (?,?,?,?,?,?)
        """, (ts, symbol, signal, price, reason, int(acted_upon)))
    update_state(last_signal_time=ts, last_signal_info=f"{signal} @ {price:.2f} — {reason}")


def get_recent_signals(limit: int = 50) -> List[Dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_signals ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------------ #
# System events (for dashboard log viewer)
# ------------------------------------------------------------------ #

def log_event(level: str, source: str, message: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock, _get_conn() as conn:
        conn.execute(
            "INSERT INTO system_events (ts, level, source, message) VALUES (?,?,?,?)",
            (ts, level, source, message)
        )
        # Keep only last 1000 events
        conn.execute("""
            DELETE FROM system_events WHERE id NOT IN (
                SELECT id FROM system_events ORDER BY id DESC LIMIT 1000
            )
        """)


def get_recent_events(limit: int = 100, level_filter: str = None) -> List[Dict]:
    with _get_conn() as conn:
        if level_filter:
            rows = conn.execute(
                "SELECT * FROM system_events WHERE level=? ORDER BY id DESC LIMIT ?",
                (level_filter, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM system_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print("DB initialized successfully.")
    print("State:", get_state())
    log_event("INFO", "test", "Database test passed")
    print("Events:", get_recent_events(5))
    print("Performance:", get_performance_summary())
    print("Test PASSED.")
