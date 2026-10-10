"""Structured JSON logs: one line per analysis and per human action, keyed by run_id."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

LOGGER = logging.getLogger("procurement")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                                   "event": record.getMessage()}
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload, default=str, sort_keys=True)


def configure(level: str = "INFO") -> None:
    """Emit JSON lines on stderr (the web app calls this; CLI and eval stay quiet unless they opt in)."""
    if any(isinstance(h.formatter, JsonFormatter) for h in LOGGER.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    LOGGER.addHandler(handler)
    LOGGER.setLevel(level)
    LOGGER.propagate = False


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    LOGGER.log(level, event, extra={"fields": fields})
