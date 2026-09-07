"""Structured JSON logging."""

import json
import logging
import re
import sys
from datetime import datetime, timezone

_SECRET_KEY_MARKERS = ("token", "key", "secret", "password")
# Scrubs credentials embedded in URLs (e.g. git's own error text echoing back
# https://x-access-token:ghp_xxx@github.com/...) — key-name redaction alone
# can't catch a secret sitting inside a string value like this.
_CREDENTIAL_URL_RE = re.compile(r"://[^/@\s]*@")


def scrub_credentials(text: str) -> str:
    """Scrub credentials embedded in URLs, e.g. in raw git/subprocess output."""
    return _CREDENTIAL_URL_RE.sub("://***@", text)


def _scrub(value):
    if isinstance(value, str):
        return scrub_credentials(value)
    if isinstance(value, dict):
        return _redact(value)
    return value


def _redact(fields: dict) -> dict:
    return {
        k: "***REDACTED***" if any(m in k.lower() for m in _SECRET_KEY_MARKERS) else _scrub(v)
        for k, v in fields.items()
    }


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
    # The MCP framework adds its own plain-text handler on the root logger;
    # without this every record ships twice (plain on stderr, JSON on stdout)
    # and log shippers see duplicated, differently-formatted events.
    logger.propagate = False

    return logger
