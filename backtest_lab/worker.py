import csv
import io
import json
import os
import sys
from datetime import datetime, timezone
from .storage import DB_PATH, get_job, set_job, save_run
from .candle_cache import load_candles
from .engine import run
from .metrics import summarize
from .strategy_registry import validate_request


def main(job_id):
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (300 * 1024 * 1024, 300 * 1024 * 1024))
    except (ImportError, ValueError):
        pass
    try:
        import signal
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("Backtest timed out after 15 minutes")))
        signal.alarm(15 * 60)
    except (ImportError, AttributeError):
        pass
    lock = DB_PATH.with_suffix(".lock")
    if lock.exists():
        set_job(job_id, status="failed", error="Another job lock exists", finished_at=datetime.now(timezone.utc).isoformat())
        return
    lock.write_text(str(os.getpid()), encoding="utf-8")
    try:
        job = get_job(job_id)
        request = json.loads(job["request_json"])
        validate_request(request)
        set_job(job_id, status="running", started_at=datetime.now(timezone.utc).isoformat(), progress=1)
        def progress(now, end):
            if get_job(job_id)["cancel_requested"]:
                raise RuntimeError("Cancelled")
            set_job(job_id, progress=min(60, int(60 * (now - request["start_ts"]) / max(1, end - request["start_ts"]))))
        candles, gaps = load_candles(request["symbol"], request["timeframe"], request["start_ts"], request["end_ts"], progress)
        set_job(job_id, progress=70, message="Running strategy")
        trades, equity = run(candles, request)
        result = summarize(trades, equity, gaps)
        split = int(len(candles) * float(request.get("in_sample_fraction", .7)))
        in_trades, in_equity = run(candles.iloc[:split].reset_index(drop=True), request)
        out_trades, out_equity = run(candles.iloc[split:].reset_index(drop=True), request)
        result["in_sample"] = summarize(in_trades, in_equity, 0)
        result["out_of_sample"] = summarize(out_trades, out_equity, 0)
        output = io.StringIO()
        if trades:
            writer = csv.DictWriter(output, fieldnames=trades[0].keys())
            writer.writeheader(); writer.writerows(trades)
        save_run(job_id, datetime.now(timezone.utc).isoformat(), request, result, output.getvalue())
        set_job(job_id, status="done", progress=100, result_json=json.dumps(result), finished_at=datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        set_job(job_id, status="failed", error=str(exc)[:300], finished_at=datetime.now(timezone.utc).isoformat())
    finally:
        lock.unlink(missing_ok=True)
        try:
            signal.alarm(0)
        except (NameError, AttributeError):
            pass


if __name__ == "__main__":
    main(int(sys.argv[1]))
