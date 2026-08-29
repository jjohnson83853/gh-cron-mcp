import json
import logging

from app.logging_config import JsonFormatter, setup_logging


def _log_record(**extra_fields) -> dict:
    logger = logging.getLogger("test_json_formatter")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = _Capture()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)

    logger.info("hello", extra={"extra_fields": extra_fields})
    return json.loads(records[0])


def test_has_required_fields():
    data = _log_record(job="demo")
    assert set(["time", "level", "logger", "message"]).issubset(data)
    assert data["message"] == "hello"
    assert data["job"] == "demo"


def test_redacts_secret_keys():
    data = _log_record(env_vars={"API_KEY": "shh", "PORT": "8000"})
    assert data["env_vars"]["API_KEY"] == "***REDACTED***"
    assert data["env_vars"]["PORT"] == "8000"


def test_setup_logging_returns_configured_logger():
    logger = setup_logging("some_logger")
    assert logger.name == "some_logger"
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0].formatter, JsonFormatter)
