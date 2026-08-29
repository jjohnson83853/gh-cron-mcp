"""Structured JSON logging."""

import json
import logging
import sys
from datetime import datetime, timezone

_SECRET_KEY_MARKERS = ("token", "key", "secret", "password")


def _redact(fields: dict) -> dict:
    result = {}
    for k, v in fields.items():
        if any(m in k.lower() for m in _SECRET_KEY_MARKERS):
            result[k] = "***REDACTED***"
        elif isinstance(v, dict):
            result[k] = _redact(v)
        else:
            result[k] = v
    return result


class JsonFormatter(logging.Formatter):
    """Formats each log record as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "time": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "extra_fields"):
            log_data.update(_redact(record.extra_fields))
        return json.dumps(log_data)


def setup_logging(name: str = "gh_cron_mcp") -> logging.Logger:
    """Configure JSON logging to stdout."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)

    return logger
