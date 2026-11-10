import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from .storage import DB_PATH, create_job, active_job, set_job
from .strategy_registry import validate_request

LOCK_PATH = DB_PATH.with_suffix(".lock")


def start(request):
    validate_request(request)
    stale_lock = False
    if LOCK_PATH.exists():
        try:
            pid = int(LOCK_PATH.read_text(encoding="utf-8"))
            os.kill(pid, 0)
        except (OSError, ValueError):
            stale_lock = True
    if stale_lock:
        LOCK_PATH.unlink(missing_ok=True)
    running = active_job()
    if running and not LOCK_PATH.exists():
        set_job(running["id"], status="failed", error="Previous worker ended without releasing its lock")
        running = None
    if running or LOCK_PATH.exists():
        raise RuntimeError("A Backtest Lab job is already running")
    job_id = create_job(datetime.now(timezone.utc).isoformat(), request)
    subprocess.Popen([sys.executable, "-m", "backtest_lab.worker", str(job_id)], cwd=str(DB_PATH.parent.parent),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return job_id


def cancel(job_id):
    set_job(job_id, cancel_requested=1, message="Cancellation requested")
