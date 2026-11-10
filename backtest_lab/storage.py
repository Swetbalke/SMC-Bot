import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "backtest_lab.db"


@contextmanager
def connection():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS candles (
          symbol TEXT NOT NULL, timeframe TEXT NOT NULL, ts INTEGER NOT NULL,
          open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL,
          PRIMARY KEY (symbol, timeframe, ts));
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
          status TEXT NOT NULL, request_json TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
          message TEXT NOT NULL DEFAULT '', result_json TEXT, error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, created_at TEXT NOT NULL,
          request_json TEXT NOT NULL, result_json TEXT NOT NULL, trades_csv TEXT NOT NULL,
          FOREIGN KEY(job_id) REFERENCES jobs(id));
        """)


def create_job(created_at, request):
    init_db()
    with connection() as conn:
        row = conn.execute("INSERT INTO jobs(created_at,status,request_json) VALUES(?,?,?)",
            (created_at, "queued", json.dumps(request, separators=(",", ":")) ))
        return row.lastrowid


def get_job(job_id):
    init_db()
    with connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def active_job():
    init_db()
    with connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def set_job(job_id, **values):
    if not values:
        return
    columns = ", ".join(f"{key}=?" for key in values)
    with connection() as conn:
        conn.execute(f"UPDATE jobs SET {columns} WHERE id=?", (*values.values(), job_id))


def cache_candles(symbol, timeframe, rows):
    if not rows:
        return
    with connection() as conn:
        conn.executemany("""INSERT OR REPLACE INTO candles(symbol,timeframe,ts,open,high,low,close,volume)
            VALUES(?,?,?,?,?,?,?,?)""", [(symbol, timeframe, *row) for row in rows])


def cached_candles(symbol, timeframe, start_ts, end_ts):
    with connection() as conn:
        rows = conn.execute("SELECT ts,open,high,low,close,volume FROM candles WHERE symbol=? AND timeframe=? AND ts>=? AND ts<? ORDER BY ts",
            (symbol, timeframe, start_ts, end_ts)).fetchall()
    return [tuple(row) for row in rows]


def save_run(job_id, created_at, request, result, trades_csv):
    with connection() as conn:
        conn.execute("INSERT INTO runs(job_id,created_at,request_json,result_json,trades_csv) VALUES(?,?,?,?,?)",
            (job_id, created_at, json.dumps(request), json.dumps(result), trades_csv))


def recent_runs(limit=20):
    init_db()
    with connection() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]
