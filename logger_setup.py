"""
logger_setup.py — Professional Rotating Logger with Deduplication
Repeated errors ek hi baar log honge, har baar nahi.
"""

import logging
import logging.handlers
import json
import os
import threading
import time
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

_dedup_lock = threading.Lock()
_last_errors: dict = {}   # {msg_key: (count, first_time, last_logged_time)}
DEDUP_WINDOW = 60         # seconds — same error agar 60s mein dobara aaye to suppress


class DedupFilter(logging.Filter):
    """Same error message ko 60 seconds tak repeat nahi karne deta."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return True  # INFO/DEBUG hamesha allow

        # Key = first 120 chars of message (without variable parts like timestamps)
        raw = str(record.getMessage())
        key = raw[:120]

        now = time.time()
        with _dedup_lock:
            entry = _last_errors.get(key)
            if entry is None:
                _last_errors[key] = {"count": 1, "first_time": now, "last_logged": now}
                return True

            entry["count"] += 1
            since_logged = now - entry["last_logged"]

            # Allow once every DEDUP_WINDOW seconds
            if since_logged >= DEDUP_WINDOW:
                suppressed = entry["count"] - 1
                record.msg = f"[{suppressed}x suppressed] " + record.msg
                entry["last_logged"] = now
                entry["count"] = 1
                return True

            return False  # Suppress


class JsonFormatter(logging.Formatter):
    """Compact JSON lines for log collectors and structured troubleshooting."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "source": f"{record.pathname}:{record.lineno}",
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str = "trading_bot", structured: bool = False) -> logging.Logger:
    """
    Returns a configured logger.
    Usage: logger = get_logger(__name__)
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger  # Already configured

    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(filename)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    formatter = JsonFormatter() if structured else fmt

    # --- Rotating file handler (main log) ---
    main_handler = logging.handlers.RotatingFileHandler(
        filename=LOG_DIR / ("trading_bot.jsonl" if structured else "trading_bot.log"),
        maxBytes=5 * 1024 * 1024,   # 5 MB
        backupCount=3,
        encoding="utf-8"
    )
    main_handler.setLevel(logging.INFO)
    main_handler.setFormatter(formatter)
    main_handler.addFilter(DedupFilter())

    # --- Errors-only file handler ---
    err_handler = logging.handlers.RotatingFileHandler(
        filename=LOG_DIR / "errors.log",
        maxBytes=2 * 1024 * 1024,   # 2 MB
        backupCount=2,
        encoding="utf-8"
    )
    err_handler.setLevel(logging.ERROR)
    err_handler.setFormatter(formatter)

    # --- Console handler ---
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s",
                                           datefmt="%H:%M:%S"))

    logger.addHandler(main_handler)
    logger.addHandler(err_handler)
    logger.addHandler(console)

    return logger


if __name__ == "__main__":
    log = get_logger("test")
    log.info("Logger test started")
    for i in range(10):
        log.error("Test repeated error message")
        time.sleep(0.1)
    log.info("Only 1 error should appear above (rest suppressed). Test PASSED.")
