"""
Structured, non-blocking logging (F-01).

THE BUG THIS REPLACES: a single logging.StreamHandler(sys.stdout) was the
only sink for ~200 threads. On Windows, an interactive console with
QuickEdit/Mark mode enabled (the default) blocks the next WriteFile to
stdout indefinitely the moment someone clicks in the window. Every thread
serializes on that one handler's lock, so RTP pacing, SIP recv loops, and
FastAPI request handling all stop -- which is what the operator experienced
as "the server hangs and Ctrl+C frees it" (Ctrl+C cancels console selection
mode, not because it delivers SIGINT).

THE FIX: no call-carrying thread ever performs blocking I/O against a log
sink. Every logger.info/... call only pushes a LogRecord onto a bounded,
non-blocking queue.Queue. A single background QueueListener thread is the
only thing that ever touches the real handler (a RotatingFileHandler on
disk). If the queue fills up (sink is somehow stuck / disk is slow), records
are dropped with a counter rather than blocking the caller -- a lost log
line is acceptable, a stalled RTP thread is not.

A console handler is only attached when LOG_TO_CONSOLE=1 (opt-in, for local
debugging only -- never enable it in production per OPS-01).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import sys
import threading


class CallContextFilter(logging.Filter):
    """Ensures every record has a call_id attribute, even if not passed via `extra`."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "call_id"):
            record.call_id = "-"
        return True


class _DroppingQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler that never blocks: on a full queue it drops the record
    and increments a counter instead of waiting for space."""

    dropped_count = 0
    _warn_lock = threading.Lock()

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            with _DroppingQueueHandler._warn_lock:
                _DroppingQueueHandler.dropped_count += 1
                # Only surface this to stderr occasionally so a saturated
                # queue can't itself become a source of blocking I/O.
                if _DroppingQueueHandler.dropped_count % 1000 == 1:
                    sys.stderr.write(
                        f"[logging] dropped {_DroppingQueueHandler.dropped_count} "
                        f"log record(s) -- log sink can't keep up\n"
                    )


_listener: "logging.handlers.QueueListener | None" = None
_log_queue: "queue.Queue | None" = None


def configure_logging(
    level: str = "INFO",
    log_dir: str = "./logs",
    log_to_console: bool = False,
    max_bytes: int = 50 * 1024 * 1024,
    backup_count: int = 10,
    queue_maxsize: int = 100_000,
) -> None:
    """Install the queue-based logging pipeline. Call once at process start,
    BEFORE any worker threads are spawned. Call stop_logging() at shutdown."""
    global _listener, _log_queue

    os.makedirs(log_dir, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | call_id=%(call_id)s | %(name)s | %(message)s",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        filename=os.path.join(log_dir, "service.log"),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(formatter)

    handlers = [file_handler]
    if log_to_console:
        # Opt-in only. See OPS-01: never rely on this in production, and
        # disable QuickEdit Mode on any console this is attached to.
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        handlers.append(console_handler)

    _log_queue = queue.Queue(maxsize=queue_maxsize)
    queue_handler = _DroppingQueueHandler(_log_queue)
    queue_handler.addFilter(CallContextFilter())

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [queue_handler]

    _listener = logging.handlers.QueueListener(_log_queue, *handlers, respect_handler_level=False)
    _listener.start()

    # Quiet down noisy third-party loggers unless we're in DEBUG.
    if level.upper() != "DEBUG":
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def stop_logging() -> None:
    """Flush and stop the background listener. Call during graceful shutdown
    so buffered records aren't lost."""
    global _listener
    if _listener is not None:
        try:
            _listener.stop()
        except Exception:
            pass
        _listener = None


def get_call_logger(call_id: str) -> logging.LoggerAdapter:
    base = logging.getLogger("call")
    return logging.LoggerAdapter(base, {"call_id": call_id})


def get_logger(name: str) -> logging.LoggerAdapter:
    return logging.LoggerAdapter(logging.getLogger(name), {"call_id": "-"})
