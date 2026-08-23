# gh-cron-mcp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** MCP server that lets an LLM schedule GitHub-hosted scripts to run on a recurring cron schedule, cloning and executing them inside the server's own container.

**Architecture:** Python FastMCP server exposing 5 tools (`add_job`, `remove_job`, `list_jobs`, `run_job_now`, `get_job_logs`), backed by an APScheduler `BackgroundScheduler` with a SQLite-backed `SQLAlchemyJobStore` for cron parsing and persistence, and a subprocess-based executor that clones/pulls the target repo via `git` and runs its entrypoint.

**Tech Stack:** Python 3.12, `mcp` (FastMCP), `apscheduler`, `sqlalchemy`, `git` CLI, Node.js runtime (for JS entrypoints), Docker.

**Spec:** `docs/superpowers/specs/2026-08-23-gh-cron-mcp-design.md`

## Global Constraints

- Python 3.12-slim base image; Node.js (nodesource 20.x) also installed for JS entrypoints.
- `cron_expr` is a standard 5-field crontab string, parsed via `apscheduler.triggers.cron.CronTrigger.from_crontab`.
- Job persistence: SQLite at `/data/scheduler.db` via APScheduler's `SQLAlchemyJobStore`; per-job last-run status at `/data/status.json`.
- `GITHUB_TOKEN` read from environment, injected into the HTTPS clone URL, never written to logs or job metadata.
- Transport is streamable-HTTP (not stdio), bound to `0.0.0.0`, port from `PORT` env var (default `8000`).
- `max_instances=1` per scheduled job — no overlapping runs of the same job.
- No per-repo GitHub Actions workflow — `docker-watcher` on `docker-lxc` builds/pushes to `registry.localdomain:5000` on commits to `main`.

---

### Task 1: Storage helpers + repo/job executor

**Files:**
- Create: `app/__init__.py` (empty)
- Create: `app/storage.py`
- Create: `app/executor.py`
- Test: `tests/test_executor.py`

**Interfaces:**
- Produces: `storage.ensure_dirs()`, `storage.repo_path(name) -> Path`, `storage.log_path(name) -> Path`, `storage.read_status() -> dict`, `storage.write_job_status(name, status: dict)`, `storage.remove_job_status(name)`, module-level `storage.DATA_DIR`, `storage.REPOS_DIR`, `storage.LOGS_DIR`, `storage.DB_PATH`, `storage.STATUS_PATH` (all `pathlib.Path`, monkeypatchable in tests).
- Produces: `executor.run_job(name: str, repo_url: str, ref: str, entrypoint: str, env_vars: dict | None, token: str | None, timeout: int = 1800) -> dict` returning `{"last_run": iso str, "success": bool, "returncode": int | None, ...}`.

- [ ] **Step 1: Create `app/__init__.py`** (empty file)

- [ ] **Step 2: Write `app/storage.py`**

```python
import json
import os
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
REPOS_DIR = DATA_DIR / "repos"
LOGS_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "scheduler.db"
STATUS_PATH = DATA_DIR / "status.json"

_status_lock = threading.Lock()


def ensure_dirs() -> None:
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def log_path(name: str) -> Path:
    return LOGS_DIR / f"{name}.log"


def repo_path(name: str) -> Path:
    return REPOS_DIR / name


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
```

- [ ] **Step 3: Write the failing test for the executor** — `tests/test_executor.py`

```python
import subprocess
from pathlib import Path

from app import executor, storage


def _init_fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fixture_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "run.sh").write_text("#!/bin/sh\necho hello-cron-test\n")
    subprocess.run(["git", "add", "run.sh"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def test_run_job_clones_and_executes(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "REPOS_DIR", tmp_path / "data" / "repos")
    monkeypatch.setattr(storage, "LOGS_DIR", tmp_path / "data" / "logs")
    monkeypatch.setattr(storage, "STATUS_PATH", tmp_path / "data" / "status.json")

    fixture_repo = _init_fixture_repo(tmp_path)

    status = executor.run_job(
        name="test-job",
        repo_url=str(fixture_repo),
        ref="main",
        entrypoint="sh run.sh",
        env_vars=None,
        token=None,
    )

    assert status["success"] is True
    assert status["returncode"] == 0
    log_content = storage.log_path("test-job").read_text()
    assert "hello-cron-test" in log_content


def test_run_job_second_call_pulls_not_reclones(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "REPOS_DIR", tmp_path / "data" / "repos")
    monkeypatch.setattr(storage, "LOGS_DIR", tmp_path / "data" / "logs")
    monkeypatch.setattr(storage, "STATUS_PATH", tmp_path / "data" / "status.json")
    fixture_repo = _init_fixture_repo(tmp_path)

    executor.run_job(name="test-job", repo_url=str(fixture_repo), ref="main", entrypoint="sh run.sh", env_vars=None, token=None)
    status = executor.run_job(name="test-job", repo_url=str(fixture_repo), ref="main", entrypoint="sh run.sh", env_vars=None, token=None)

    assert status["success"] is True
    log_lines = storage.log_path("test-job").read_text().count("hello-cron-test")
    assert log_lines == 2
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/test_executor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.executor'` (or `ImportError`).

- [ ] **Step 5: Write `app/executor.py`**

```python
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import storage


def _clone_url_with_token(repo_url: str, token: Optional[str]) -> str:
    if not token or not repo_url.startswith("https://"):
        return repo_url
    return repo_url.replace("https://", f"https://x-access-token:{token}@", 1)


def sync_repo(repo_url: str, ref: str, dest: Path, token: Optional[str]) -> None:
    auth_url = _clone_url_with_token(repo_url, token)
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--branch", ref, "--depth", "1", auth_url, str(dest)],
            check=True, capture_output=True, text=True,
        )
    else:
        subprocess.run(["git", "remote", "set-url", "origin", auth_url], cwd=dest, check=True, capture_output=True, text=True)
        subprocess.run(["git", "fetch", "--depth", "1", "origin", ref], cwd=dest, check=True, capture_output=True, text=True)
        subprocess.run(["git", "checkout", ref], cwd=dest, check=True, capture_output=True, text=True)
        subprocess.run(["git", "reset", "--hard", f"origin/{ref}"], cwd=dest, check=True, capture_output=True, text=True)


def run_job(
    name: str,
    repo_url: str,
    ref: str,
    entrypoint: str,
    env_vars: Optional[dict],
    token: Optional[str],
    timeout: int = 1800,
) -> dict:
    storage.ensure_dirs()
    dest = storage.repo_path(name)
    log_file = storage.log_path(name)
    started = datetime.now(timezone.utc).isoformat()
    env = {**os.environ, **(env_vars or {})}

    try:
        sync_repo(repo_url, ref, dest, token)
        result = subprocess.run(
            entrypoint, shell=True, cwd=dest, env=env,
            capture_output=True, text=True, timeout=timeout,
        )
        with open(log_file, "a") as f:
            f.write(f"\n=== run {started} ===\n")
            f.write(result.stdout)
            f.write(result.stderr)
        status = {
            "last_run": started,
            "success": result.returncode == 0,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        status = {"last_run": started, "success": False, "returncode": None, "error": "timeout"}
        with open(log_file, "a") as f:
            f.write(f"\n=== run {started} TIMED OUT after {timeout}s ===\n")
    except subprocess.CalledProcessError as e:
        status = {"last_run": started, "success": False, "returncode": e.returncode, "error": e.stderr}
        with open(log_file, "a") as f:
            f.write(f"\n=== run {started} git sync failed ===\n{e.stderr}\n")

    storage.write_job_status(name, status)
    return status
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_executor.py -v`
Expected: PASS (2 tests)

- [ ] **Step 7: Commit**

```bash
git add app/__init__.py app/storage.py app/executor.py tests/test_executor.py
git commit -m "feat: add job executor with git sync + subprocess run"
```

---

### Task 2: Scheduler service (cron parsing + persistence)

**Files:**
- Create: `app/scheduler_service.py`
- Test: `tests/test_scheduler_service.py`

**Interfaces:**
- Consumes: `executor.run_job(name, repo_url, ref, entrypoint, env_vars, token, timeout=1800) -> dict` (Task 1), `storage.ensure_dirs()`, `storage.repo_path`, `storage.log_path`, `storage.read_status`, `storage.remove_job_status`, `storage.DB_PATH` (Task 1).
- Produces: `class JobNotFound(Exception)`; `class SchedulerService.__init__(self, db_path: str | None = None, github_token: str | None = None)`; methods `add_job(name, repo_url, ref, entrypoint, cron_expr, env_vars=None) -> dict`, `remove_job(name) -> None`, `list_jobs() -> list[dict]`, `run_job_now(name) -> dict`, `get_job_logs(name, lines=100) -> str`, `shutdown()`. Each job dict: `{"name": str, "next_run_time": str | None, "last_run": str | None, "success": bool | None}`.

- [ ] **Step 1: Write the failing test** — `tests/test_scheduler_service.py`

```python
import pytest

from app import storage
from app.scheduler_service import JobNotFound, SchedulerService


def _patch_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "REPOS_DIR", tmp_path / "repos")
    monkeypatch.setattr(storage, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(storage, "STATUS_PATH", tmp_path / "status.json")


def test_add_job_persists_across_restart(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    db_path = str(tmp_path / "scheduler.db")

    svc = SchedulerService(db_path=db_path)
    info = svc.add_job(
        name="nightly", repo_url="https://example.invalid/repo.git", ref="main",
        entrypoint="true", cron_expr="0 2 * * *",
    )
    assert info["name"] == "nightly"
    assert info["next_run_time"] is not None
    svc.shutdown()

    svc2 = SchedulerService(db_path=db_path)
    jobs = svc2.list_jobs()
    assert [j["name"] for j in jobs] == ["nightly"]
    svc2.shutdown()


def test_remove_job_raises_job_not_found(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))
    with pytest.raises(JobNotFound):
        svc.remove_job("does-not-exist")
    svc.shutdown()


def test_invalid_cron_expression_raises(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))
    with pytest.raises(ValueError):
        svc.add_job(name="bad", repo_url="https://x/y.git", ref="main", entrypoint="true", cron_expr="not a cron")
    svc.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scheduler_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.scheduler_service'`

- [ ] **Step 3: Write `app/scheduler_service.py`**

```python
from typing import Optional

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import executor, storage


class JobNotFound(Exception):
    pass


class SchedulerService:
    def __init__(self, db_path: Optional[str] = None, github_token: Optional[str] = None):
        storage.ensure_dirs()
        db_path = db_path or str(storage.DB_PATH)
        self._scheduler = BackgroundScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{db_path}")}
        )
        self._token = github_token
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    def add_job(self, name: str, repo_url: str, ref: str, entrypoint: str, cron_expr: str, env_vars: Optional[dict] = None) -> dict:
        trigger = CronTrigger.from_crontab(cron_expr)
        self._scheduler.add_job(
            executor.run_job,
            trigger=trigger,
            id=name,
            replace_existing=True,
            max_instances=1,
            kwargs={
                "name": name, "repo_url": repo_url, "ref": ref,
                "entrypoint": entrypoint, "env_vars": env_vars, "token": self._token,
            },
        )
        return self._job_info(name)

    def remove_job(self, name: str) -> None:
        job = self._scheduler.get_job(name)
        if job is None:
            raise JobNotFound(name)
        self._scheduler.remove_job(name)
        storage.remove_job_status(name)

    def list_jobs(self) -> list:
        return [self._job_info(job.id) for job in self._scheduler.get_jobs()]

    def run_job_now(self, name: str) -> dict:
        job = self._scheduler.get_job(name)
        if job is None:
            raise JobNotFound(name)
        return executor.run_job(**job.kwargs)

    def get_job_logs(self, name: str, lines: int = 100) -> str:
        log_file = storage.log_path(name)
        if not log_file.exists():
            return ""
        return "\n".join(log_file.read_text().splitlines()[-lines:])

    def _job_info(self, name: str) -> dict:
        job = self._scheduler.get_job(name)
        if job is None:
            raise JobNotFound(name)
        status = storage.read_status().get(name, {})
        return {
            "name": job.id,
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            "last_run": status.get("last_run"),
            "success": status.get("success"),
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_scheduler_service.py -v`
Expected: PASS (3 tests). Note: `CronTrigger.from_crontab` already raises `ValueError` on a malformed expression, so no extra validation code is needed.

- [ ] **Step 5: Commit**

```bash
git add app/scheduler_service.py tests/test_scheduler_service.py
git commit -m "feat: add APScheduler-backed scheduler service"
```

---

### Task 3: MCP tool layer

**Files:**
- Create: `app/job_tools.py`
- Create: `app/server.py`
- Test: `tests/test_job_tools.py`

**Interfaces:**
- Consumes: `SchedulerService` public methods (Task 2) — `add_job`, `remove_job`, `list_jobs`, `run_job_now`, `get_job_logs`, and `JobNotFound` exception.
- Produces: `class JobTools.__init__(self, scheduler: SchedulerService)` with methods `add_job(name, repo_url, cron_expr, entrypoint, ref="main", env_vars=None) -> dict`, `remove_job(name) -> str`, `list_jobs() -> list`, `run_job_now(name) -> dict`, `get_job_logs(name, lines=100) -> str`. Produces: `build_server(scheduler: SchedulerService) -> FastMCP` (registers the 5 tools by name: `add_job`, `remove_job`, `list_jobs`, `run_job_now`, `get_job_logs`).

- [ ] **Step 1: Write the failing test** — `tests/test_job_tools.py`

```python
from unittest.mock import MagicMock

from app.job_tools import JobTools
from app.scheduler_service import JobNotFound


def test_add_job_delegates_to_scheduler():
    scheduler = MagicMock()
    scheduler.add_job.return_value = {"name": "nightly"}
    tools = JobTools(scheduler)

    result = tools.add_job(name="nightly", repo_url="https://x/y.git", cron_expr="0 2 * * *", entrypoint="python main.py")

    scheduler.add_job.assert_called_once_with("nightly", "https://x/y.git", "main", "python main.py", "0 2 * * *", None)
    assert result == {"name": "nightly"}


def test_remove_job_handles_not_found():
    scheduler = MagicMock()
    scheduler.remove_job.side_effect = JobNotFound("ghost")
    tools = JobTools(scheduler)

    result = tools.remove_job("ghost")

    assert result == "no job named 'ghost'"


def test_run_job_now_handles_not_found():
    scheduler = MagicMock()
    scheduler.run_job_now.side_effect = JobNotFound("ghost")
    tools = JobTools(scheduler)

    result = tools.run_job_now("ghost")

    assert result == {"error": "no job named 'ghost'"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_job_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.job_tools'`

- [ ] **Step 3: Write `app/job_tools.py`**

```python
from typing import Optional

from .scheduler_service import JobNotFound, SchedulerService


class JobTools:
    def __init__(self, scheduler: SchedulerService):
        self._scheduler = scheduler

    def add_job(self, name: str, repo_url: str, cron_expr: str, entrypoint: str, ref: str = "main", env_vars: Optional[dict] = None) -> dict:
        return self._scheduler.add_job(name, repo_url, ref, entrypoint, cron_expr, env_vars)

    def remove_job(self, name: str) -> str:
        try:
            self._scheduler.remove_job(name)
        except JobNotFound:
            return f"no job named {name!r}"
        return f"removed {name!r}"

    def list_jobs(self) -> list:
        return self._scheduler.list_jobs()

    def run_job_now(self, name: str) -> dict:
        try:
            return self._scheduler.run_job_now(name)
        except JobNotFound:
            return {"error": f"no job named {name!r}"}

    def get_job_logs(self, name: str, lines: int = 100) -> str:
        return self._scheduler.get_job_logs(name, lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_job_tools.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Write `app/server.py`** (no dedicated test — thin registration wiring over already-tested `JobTools`)

Note: `mcp` 2.0.0 renamed `FastMCP` to `MCPServer` and moved it to `mcp.server.mcpserver` (confirmed against the installed package — `mcp.server.fastmcp` no longer exists as of this version).

```python
from mcp.server.mcpserver import MCPServer

from .job_tools import JobTools
from .scheduler_service import SchedulerService


def build_server(scheduler: SchedulerService) -> MCPServer:
    mcp = MCPServer("gh-cron-mcp")
    tools = JobTools(scheduler)

    mcp.add_tool(tools.add_job, name="add_job", description="Schedule a GitHub repo's script to run on a recurring cron schedule.")
    mcp.add_tool(tools.remove_job, name="remove_job", description="Remove a scheduled job by name.")
    mcp.add_tool(tools.list_jobs, name="list_jobs", description="List all scheduled jobs with next run time and last status.")
    mcp.add_tool(tools.run_job_now, name="run_job_now", description="Trigger an immediate out-of-schedule run of a job.")
    mcp.add_tool(tools.get_job_logs, name="get_job_logs", description="Get the tail of a job's execution log.")
    return mcp
```

- [ ] **Step 6: Write `app/main.py`**

```python
import os

from .scheduler_service import SchedulerService
from .server import build_server


def main() -> None:
    scheduler = SchedulerService(github_token=os.environ.get("GITHUB_TOKEN"))
    mcp = build_server(scheduler)
    mcp.run(transport="streamable-http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Commit**

```bash
git add app/job_tools.py app/server.py app/main.py tests/test_job_tools.py
git commit -m "feat: wire MCP tools (add_job/remove_job/list_jobs/run_job_now/get_job_logs)"
```

---

### Task 4: Dependencies + Dockerfile

**Files:**
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `Dockerfile`
- Create: `.dockerignore`

**Interfaces:**
- Consumes: `app/main.py` (Task 3) as container `CMD` entrypoint.
- Produces: buildable image tagged `gh-cron-mcp:test` for local verification; final image published by docker-watcher as `registry.localdomain:5000/gh-cron-mcp:latest`.

- [ ] **Step 1: Write `requirements.txt`**

```
mcp==2.0.0
apscheduler>=3.10,<4
sqlalchemy>=2.0
```

- [ ] **Step 2: Write `requirements-dev.txt`**

```
-r requirements.txt
pytest>=8.0
```

- [ ] **Step 3: Write `.dockerignore`**

```
.git
docs/
tests/
__pycache__/
*.pyc
.pytest_cache/
```

- [ ] **Step 4: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV DATA_DIR=/data
VOLUME /data

CMD ["python", "-m", "app.main"]
```

- [ ] **Step 5: Build and smoke-test the image**

Run:
```bash
docker build -t gh-cron-mcp:test .
docker run -d --name gh-cron-mcp-smoke -p 8099:8000 -e PORT=8000 gh-cron-mcp:test
sleep 3
docker logs gh-cron-mcp-smoke
docker ps --filter name=gh-cron-mcp-smoke --filter status=running -q
```
Expected: `docker logs` shows the server starting with no traceback; the `docker ps` filter returns a non-empty container ID (still running).

- [ ] **Step 6: Tear down the smoke container**

```bash
docker rm -f gh-cron-mcp-smoke
```

- [ ] **Step 7: Commit**

```bash
git add requirements.txt requirements-dev.txt Dockerfile .dockerignore
git commit -m "build: add Dockerfile and Python dependencies"
```

---

### Task 5: Compose deliverable + PR template

**Files:**
- Create: `docker-compose.yml`
- Create: `.env.template`
- Create: `.github/PULL_REQUEST_TEMPLATE.md`

**Interfaces:**
- Consumes: image `registry.localdomain:5000/gh-cron-mcp:latest` (produced by docker-watcher from Task 4's Dockerfile), env vars `GITHUB_TOKEN`, `PORT` (Task 3's `app/main.py`).

- [ ] **Step 1: Write `docker-compose.yml`**

```yaml
services:
  gh-cron-mcp:
    image: registry.localdomain:5000/gh-cron-mcp:latest
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      - GITHUB_TOKEN=${GITHUB_TOKEN}
      - PORT=8000
    volumes:
      - gh-cron-mcp-data:/data

volumes:
  gh-cron-mcp-data:
```

- [ ] **Step 2: Write `.env.template`**

```
GITHUB_TOKEN=
```

- [ ] **Step 3: Write `.github/PULL_REQUEST_TEMPLATE.md`**

```markdown
## Summary


## Changes


## Test plan

- [ ]
```

- [ ] **Step 4: Validate compose syntax**

Run: `docker compose config`
Expected: prints the resolved config with no errors (a blank `GITHUB_TOKEN` warning is fine — it's meant to come from `.env`).

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml .env.template .github/PULL_REQUEST_TEMPLATE.md
git commit -m "infra: add compose deployment deliverable and PR template"
```

---

## Post-plan (not part of this plan's tasks)

Push `main` to a new GitHub repo named `gh-cron-mcp` so `docker-watcher` picks it up and starts building/pushing to `registry.localdomain:5000`. Not automated here since it requires the user's GitHub org/account target.
