"""Logging setup."""

from __future__ import annotations

import json
import logging


class _StructuredFormatter(logging.Formatter):
    """JSON formatter for structured log aggregators (ELK, Loki, etc.)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload)


def setup_logging(level: str, json_format: bool = False) -> None:
    """Configure the root logger.

    Args:
        level: log level string, e.g. "INFO", "DEBUG".
        json_format: emit structured JSON lines when True.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler()
    if json_format:
        handler.setFormatter(_StructuredFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s  %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
    root.handlers = [handler]
