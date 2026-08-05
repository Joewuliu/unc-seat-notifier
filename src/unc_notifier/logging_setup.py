"""Structured (JSON-lines) logging to a file, stdlib ``logging`` only.

Every poll, state change, and error goes through the standard logging calls in
``poller.py``; this module only controls how those records are formatted and where
they go.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

_RESERVED = logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and key != "message"
        }
        if extras:
            payload["extra"] = extras
        return json.dumps(payload, default=str)


def configure_logging(log_path: str | Path, *, level: int = logging.INFO) -> None:
    """Configure the root logger once. Safe to call multiple times (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level)

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    for handler in list(root.handlers):
        if getattr(handler, "_unc_notifier_handler", False):
            root.removeHandler(handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    file_handler._unc_notifier_handler = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)
