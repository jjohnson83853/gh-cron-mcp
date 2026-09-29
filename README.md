# gh-cron-mcp

An MCP server that schedules a GitHub repository's script to run on a
recurring cron schedule. Jobs clone the repo (shallow), run a shell
entrypoint inside the checkout, capture the log tail, and persist status —
driven entirely through MCP tools, so an agent can manage its own scheduled
workloads.

Transport: FastMCP **streamable-HTTP** at `/mcp`, stateless, JSON responses.
Also serves `GET /health`. No built-in auth — front with HTTPS at your
reverse proxy if exposing beyond localhost.

## How jobs run

Every job execution:

1. Clones `repo_url` at `ref` (shallow, depth 1) into `/data/repos/<name>`
   — first run clones, later runs `fetch` + `reset --hard origin/<ref>` so
   the checkout is always current and the working dir persists between runs
   (a venv you create in the entrypoint survives).
2. Runs `entrypoint` with `subprocess.run(..., shell=True)` from the repo
   root. `env_vars` are merged over the server's own environment.
3. Timeout is **1800 s** (30 min) per run. On timeout, running containers
   the job started (when `DOCKER_HOST` is set in its env) are reaped.
4. stdout+stderr are appended to the job's log (credentials scrubbed);
   last status (returncode, duration, commit) is queryable via
   `list_jobs` / `get_job_logs`.
5. Same job name never runs concurrently (manual triggers included).

Auth for private repos: HTTPS URLs get `GITHUB_TOKEN` injected as
`x-access-token`; `git@github.com:` SSH URLs use the key mounted at
`/root/.ssh/id_ed25519`.

## Tools

| Tool | Args | Purpose |
|---|---|---|
| `add_job` | `name`, `repo_url`, `cron_expr`, `entrypoint`, `ref="main"`, `env_vars` | Schedule a repo's script |
| `remove_job` | `name` | Delete a job |
| `list_jobs` | — | Schedule, paused state, next run, last status |
| `run_job_now` | `name` | Immediate out-of-schedule run |
| `get_job_logs` | `name`, `lines=100` | Tail of execution log |
| `update_job_schedule` | `name`, `cron_expr` | Change schedule only |
| `pause_job` / `resume_job` | `name` | Suspend/resume; manual runs still work while paused |

## Install

### Docker (the intended deployment)

```bash
git clone git@github.com:jjohnson83853/gh-cron-mcp.git
cd gh-cron-mcp

# compose pulls registry.localdomain:5000/gh-cron-mcp:latest — build & push first
docker build -t registry.localdomain:5000/gh-cron-mcp:latest .
docker push registry.localdomain:5000/gh-cron-mcp:latest

# SSH access for private-repo jobs (paths configurable)
mkdir -p secrets
cp ~/.ssh/id_ed25519 secrets/
ssh-keyscan github.com > secrets/known_hosts

GITHUB_TOKEN=ghp_xxx docker compose up -d
curl -f http://localhost:8000/health
```

`docker-compose.yml` mounts the `/data` volume (job store + repo checkouts
+ logs — survives restarts), injects `GITHUB_TOKEN` (used for HTTPS clones
and persisted job-store recovery), and optionally maps
`SSH_PRIVATE_KEY_PATH` / `SSH_KNOWN_HOSTS_PATH` to non-default key paths.
Set `PORT` in the compose `environment:` block to change the port.

### Local (no Docker)

Python 3.11+ (server runs on 3.12 in Docker; deps: `mcp`, `apscheduler`,
`sqlalchemy`):

```bash
pip install -r requirements.txt
GITHUB_TOKEN=ghp_xxx PORT=8000 python -m app.main
# → http://0.0.0.0:8000/mcp  (health: http://0.0.0.0:8000/health)
```

### MCP client registration

Streamable-HTTP, stateless, no auth headers:

```json
{
  "mcpServers": {
    "gh-cron": { "url": "http://localhost:8000/mcp" }
  }
}
```

Point the URL at your HTTPS-fronted hostname when running beyond localhost.

## Example: scheduling taxprep with gh-cron

Register the [taxprep](https://github.com/jjohnson83853/taxprep) pipeline
as a monthly job:

```
add_job(
  name="taxprep",
  repo_url="git@github.com:jjohnson83853/taxprep.git",   # or HTTPS + GITHUB_TOKEN
  cron_expr="0 6 1 * *",                                  # 06:00 on the 1st
  ref="main",
  entrypoint=(
    "python3 -m venv .venv"
    " && .venv/bin/pip install -e ."
    " && .venv/bin/playwright install --with-deps chromium"
    " && .venv/bin/taxprep-recon run --period $(date +%Y-%m)"
  ),
  env_vars={"TAXPREP_GOOGLE_CREDENTIALS": "/secrets/service-account.json"}
)
```

What happens each month: fresh checkout → idempotent venv bootstrap
(`pip install -e .` is a no-op after the first run since the checkout
persists in `/data/repos/taxprep`) → reconciliation run for the current
period → log tail available via `get_job_logs("taxprep")`.

Prerequisites for the taxprep job (do these once on the host):

- **Google service account**: mount the JSON and point
  `TAXPREP_GOOGLE_CREDENTIALS` at it (overrides the `config.toml`
  `credentials` path). The `config.toml` committed to the repo carries the
  real `spreadsheet_id` — placeholder values in the repo will fail the run.
- **Data files**: `taxprep` (the full pipeline) reads `inbox/amazon*.zip`,
  `inbox/quickbooks*.csv`, and scrapes Lowe's (needs an interactive
  `--login` session first). `taxprep-recon` needs none of that and is the
  right cron target for reconciliation-only schedules.
- **Verify before scheduling**: trigger manually once —
  `run_job_now("taxprep")` then `get_job_logs("taxprep", lines=50)`.

Change cadence without touching the entrypoint:
`update_job_schedule("taxprep", "0 6 15 * *")`; stop it temporarily with
`pause_job("taxprep")` (manual `run_job_now` still works while paused).