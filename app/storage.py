import json
import os
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
REPOS_DIR = DATA_DIR / "repos"
LOGS_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "scheduler.db"
STATUS_PATH = DATA_DIR / "status.json"

LOG_CAP_BYTES = 5_000_000

_status_lock = threading.Lock()
_log_lock = threading.Lock()


def ensure_dirs() -> None:
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def log_path(name: str) -> Path:
    return LOGS_DIR / f"{name}.log"


def repo_path(name: str) -> Path:
    return REPOS_DIR / name


def append_log(name: str, text: str) -> None:
    """Append to a job's log file, dropping the oldest half once it exceeds LOG_CAP_BYTES.

    Nothing rotated this before — an unbounded per-job log eventually fills
    the data volume and produces unrelated-looking failures elsewhere.
    """
    path = log_path(name)
    with _log_lock:
        with open(path, "a") as f:
            f.write(text)
        if path.stat().st_size > LOG_CAP_BYTES:
            data = path.read_bytes()[-(LOG_CAP_BYTES // 2):]
            path.write_bytes(b"...[truncated]...\n" + data)


def read_status() -> dict:
    with _status_lock:
        if not STATUS_PATH.exists():
            return {}
        return json.loads(STATUS_PATH.read_text())


def write_job_status(name: str, status: dict) -> None:
    with _status_lock:
        data = {}
        if STATUS_PATH.exists():
            data = json.loads(STATUS_PATH.read_text())
        data[name] = status
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text(json.dumps(data))


def remove_job_status(name: str) -> None:
    with _status_lock:
        if not STATUS_PATH.exists():
            return
        data = json.loads(STATUS_PATH.read_text())
        data.pop(name, None)
        STATUS_PATH.write_text(json.dumps(data))
