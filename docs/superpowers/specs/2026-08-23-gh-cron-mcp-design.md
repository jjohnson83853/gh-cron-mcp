# gh-cron-mcp — Design

## Purpose

An MCP server that lets an LLM schedule scripts/programs pulled from GitHub to
run on a recurring basis, inside a container it controls. The LLM configures
jobs (repo, entrypoint, cron schedule) through MCP tool calls; the server owns
scheduling, execution, and log retention.

## Approach

Python + the official MCP Python SDK (FastMCP) for the server, APScheduler for
scheduling and job persistence. Rejected alternatives: Node.js stack (weaker
built-in persistent job store than APScheduler's `SQLAlchemyJobStore`), and
shelling out to real system cron (adds a second scheduler daemon and a
file-reload step for no benefit over an in-process library).

## Components

### MCP tools

- `add_job(name, repo_url, ref="main", entrypoint, cron_expr, env_vars=None)`
  — clones the repo, registers a scheduled job. `cron_expr` is a standard
  5-field cron string, parsed via `CronTrigger.from_crontab()`.
- `remove_job(name)` — unregisters the job, leaves cloned repo/logs on disk.
- `list_jobs()` — name, schedule, next run time, last run status.
- `run_job_now(name)` — triggers an out-of-band run.
- `get_job_logs(name, lines=100)` — tail of the job's log file.

### Scheduler

APScheduler `BackgroundScheduler`, `SQLAlchemyJobStore` backed by SQLite at
`/data/scheduler.db`. Job persistence across container restarts comes free
from the job store — no custom persistence code. `max_instances=1` per job
(no overlapping runs of the same job); default misfire grace time.

### Job execution

On trigger:
1. `git clone` (first run) or `git pull` (subsequent) into
   `/data/repos/<name>`, using `GITHUB_TOKEN` injected into the clone URL.
   Token is read from environment, never written to logs or job metadata.
2. Run `entrypoint` (e.g. `python main.py`, `./run.sh`, `node index.js`) via
   `subprocess.run` with a timeout, cwd set to the repo checkout.
3. stdout/stderr appended to `/data/logs/<name>.log`.

### Container

`python:3.12-slim` base + `nodejs`/`npm` + `git`, so cloned repos can be
Python or Node projects (or plain shell). Server listens over streamable
HTTP/SSE (not stdio) on a configurable port, matching how the other MCP
servers in this stack (mcp-mqtt, mcp-netutils, etc.) get aggregated through
metamcp.

### Persistence

Docker volume mounted at `/data`: scheduler DB, cloned repos, job logs. All
survive container restart/redeploy.

### Deploy

New repo (`gh-cron-mcp`), `Dockerfile`, no per-repo GitHub Actions workflow —
picked up automatically by the `docker-watcher` service on `docker-lxc`
(builds/pushes to `registry.localdomain:5000` on commits to `main`).
Deliverables: `docker-compose.yml` + `.env.template` (`GITHUB_TOKEN`, `PORT`),
`.github/PULL_REQUEST_TEMPLATE.md`.

## Known ceiling

Jobs run with container-level isolation only — no per-job sandboxing
(no gVisor / user-namespace separation between jobs sharing the container).
Acceptable because this is a trusted-scripts scheduler, not a multi-tenant
sandbox. Upgrade path if that assumption ever changes: run each job in its
own ephemeral container instead of a subprocess.

## Testing

Non-trivial logic gets one runnable self-check, no framework:
- Cron parsing / job registration: `assert`-based check that a known cron
  string produces the expected next-run time via APScheduler's trigger.
- Job execution: a `test_*.py` that runs the executor against a tiny local
  fixture "repo" (no network) and asserts log output + exit status handling.
