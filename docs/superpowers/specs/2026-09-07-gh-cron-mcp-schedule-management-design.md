# gh-cron-mcp — Schedule Management (Update / Pause / Resume) — Design

**Date:** 2026-09-07
**Status:** Draft — pending user confirmation (SpecDD checkpoint 2).
Checkpoint 1 decisions confirmed by user 2026-09-07 and folded in (see D1
note and D7).
**Requirements:** `docs/superpowers/specs/2026-09-07-gh-cron-mcp-schedule-management-requirements.md`
(referenced below as FR1–FR8; not restated here)

## Environment findings

All verified by reading the current source (mcp-codebase-searcher, project
`gh-cron-mcp`), unless labeled *assumption*:

- `SchedulerService.__init__` (`app/scheduler_service.py:19`) starts a
  `BackgroundScheduler` with a SQLite-backed `SQLAlchemyJobStore` at
  `db_path`. Jobs persist across restarts — proven by
  `tests/test_scheduler_service.py::test_add_job_persists_across_restart`.
- `add_job` (`scheduler_service.py:51`) registers `executor.run_job` with
  `id=name`, `replace_existing=True`, `max_instances=1`, and persists
  `kwargs={name, repo_url, ref, entrypoint, env_vars, token}` in the
  jobstore. `CronTrigger.from_crontab` parses/validates the expression and
  raises `ValueError` on garbage (proven by
  `test_invalid_cron_expression_raises`).
- `_job_info` (`scheduler_service.py:98`) returns
  `{name, next_run_time, last_run, success}`. `next_run_time is None` is
  exactly how APScheduler 3.x represents a paused job (`pause_job` stores
  `next_run_time=None` in the jobstore; the run loop skips such jobs).
- `JobNotFound` (`scheduler_service.py:14`) is the established unknown-name
  exception; `JobTools` (`app/job_tools.py`) maps it to clean messages,
  `run_job_now` maps exceptions to `{"error": …}` dicts.
- Tool registration lives in `build_server` (`app/server.py:9`) via
  `mcp.add_tool(tools.<method>, name=…, description=…)`.
- `run_job_now` (`scheduler_service.py:85`) calls `executor.run_job(**job.kwargs)`
  directly — it is independent of the scheduler's firing loop, so it is
  unaffected by paused state (FR8 comes for free).
- `docker-compose.yml`: always-on service, `restart: unless-stopped`, named
  volume at `/data` holding `scheduler.db`. → Watchtower handles image
  pickup; no compose changes needed.
- Pinned `apscheduler>=3.10,<4` (`requirements.txt`) — 3.x API
  (`pause_job`/`resume_job`/`add_job(next_run_time=None)`).

**Mechanism claims to be proven empirically by tests, not trusted from
docs** (APScheduler 4.x docs are easy to confuse with 3.x):

- Paused state (`next_run_time=None`) persists across scheduler restart in
  `SQLAlchemyJobStore` → AC7.2 gets a dedicated restart test.
- `add_job(…, replace_existing=True, next_run_time=None)` registers a job
  directly as paused — this is the documented 3.x way to add a paused job,
  and it lets update-on-paused-job re-pause *atomically* (no un-paused
  window). *Assumption until the tests pass; fallback if it misbehaves:
  re-add actively then call `pause_job(name)` immediately after.*

## ADRs

### D1 — Three dedicated tools (FR1–FR3) — user-confirmed 2026-09-07

`update_job_schedule(name, cron_expr)`, `pause_job(name)`, `resume_job(name)`.

- **Rejected: `set_job_state(name, enabled: bool)`** — boolean polarity is a
  classic LLM footgun ("enabled: false to disable… or does false mean
  disabled?"), and the tool list no longer self-documents capability.
- **Rejected: generic `update_job(name, cron_expr=None, entrypoint=None, …)`**
  — invites the scope creep requirements explicitly exclude (non-goals), and
  each optional param adds a validation/combination path.
- Matches the existing verb-per-tool convention (`add/remove/list/run/get`).

### D2 — Pause = APScheduler-native `pause_job`, no custom flag (FR2, FR7)

`SchedulerService.pause_job` delegates to the scheduler's `pause_job(name)`;
paused state is `next_run_time=None` in the jobstore — the same persistence
mechanism already proven for jobs themselves.

- **Rejected: `enabled` flag in `status.json`** — a second source of truth
  that can drift from the jobstore (e.g. crash between the two writes), and
  it would require startup reconciliation code to decide whether a stored
  "enabled" job should be paused in the scheduler. The jobstore already
  encodes the state and survives restarts.
- Free behaviors we'd otherwise re-implement: no fire-on-boot while paused,
  no catch-up burst on resume (resume recomputes the next occurrence from
  *now*).

### D3 — Update = validate-then-re-add through the existing registration path (FR1, FR6)

`update_job_schedule(name, cron_expr)`:

1. `job = scheduler.get_job(name)`; `None` → raise `JobNotFound` (before any
   mutation — FR1 AC1.4).
2. Read `repo_url`/`ref`/`entrypoint`/`env_vars` from the stored
   `job.kwargs`. **Do not reuse the stored `token`** — substitute the
   current `self._token` (env-injected at startup), so a rotated
   `GITHUB_TOKEN` takes effect on update.
3. `was_paused = job.next_run_time is None`.
4. `CronTrigger.from_crontab(cron_expr)` — `ValueError` propagates *before*
   any re-add, so an invalid expression leaves the job untouched (FR1 AC1.5).
5. Re-register via the same `add_job(…, replace_existing=True)` code path
   with the preserved kwargs + new trigger. If `was_paused`, pass
   `next_run_time=None` so the job lands paused atomically (D2 mechanism).
6. Log `"job schedule updated"` with `{job, cron_expr, paused}` — **never
   `env_vars`** (they may hold credentials; see threat model). Return
   `_job_info(name)`.

- **Rejected: `scheduler.modify_job(name, trigger=…)`** — leaves the stale
  token frozen in stored kwargs, and still recalculates `next_run_time`, so
  it buys none of the pause-preservation simplicity it appears to.
- **Rejected: remove+add** — `remove_job` deletes run status (exactly the
  damage this feature exists to prevent) and momentarily unregisters the
  job (a fire-during-window is theoretical but the delete of status is not).

### D4 — `paused` + `cron_expr` in `_job_info` (FR4)

- `paused: bool` = `job.next_run_time is None`.
- `cron_expr` is **derived from the trigger**, by field *name* (not list
  position, which is an APScheduler internal ordering):
  `{"minute","hour","day","month","day_of_week"}` → join their `str(field)`
  renderings. `from_crontab`-created triggers round-trip their 5 fields.
  Normalized rendering is acceptable per AC4.2.
- **Rejected: storing `cron_expr` in the job's `description` field** — a
  second value to keep in sync on every write path; derivation reads from
  the single source of truth (the trigger).
- Additive keys → backward compatible (existing consumers see supersets).

### D5 — Error mapping at the `JobTools` layer (FR5)

`SchedulerService` raises typed exceptions (`JobNotFound`; `ValueError` from
`from_crontab`, same as `add_job` today). `JobTools` converts them to the
existing error surface:

- `JobNotFound` → `{"error": "no job named 'x'"}` (mirrors `run_job_now`).
- `ValueError` on cron parse → `{"error": "invalid cron_expr '…': <msg>"}`
  (naming the input, per AC5.2).
- Success → the job-info dict (so the LLM sees the resulting schedule +
  paused state in the same call).

`pause_job`/`resume_job` in the service do their own `get_job is None` check
first (matching `run_job_now`), rather than translating APScheduler's
`JobLookupError` — one idiom in the file.

### D6 — `run_job_now` unchanged on paused jobs (FR8)

`run_job_now` already bypasses the firing loop (`executor.run_job(**job.kwargs)`).
Pausing stores `next_run_time=None`; it does not touch stored kwargs.
Document-as-intended + one regression test; no code change.

### D7 — `env_vars` log redaction in `add_job` (FR9, user-approved scope addition)

The existing `add_job` log event (`scheduler_service.py:69`) writes
`env_vars` verbatim. Fix: log the **sorted list of env-var names only**
(`env_vars: ["KEY_A", "KEY_B"]`); values are never serialized to any log
record. Name-only was chosen over value-masking because names alone answer
the operational question ("what config does this job carry?") with zero
leak risk and no masking-format edge cases (empty values, values that look
like `[REDACTED]`, etc.).

- **Rejected: masked values (`"KEY": "***"`)** — adds a redaction format to
  maintain and invites partial-leak bugs for no extra operational value.
- **Rejected: dropping `env_vars` from the log entirely** — loses the
  names-only observability for free.
- New log events (`job paused`, `job resumed`, `job schedule updated`)
  already log only `{job, cron_expr, paused}` — consistent with D7 by
  construction.

## API summary

MCP tools (registered in `build_server`, `app/server.py`):

```
update_job_schedule(name: str, cron_expr: str) -> dict   # job info incl. new cron_expr + paused
pause_job(name: str) -> dict                             # job info, paused: true
resume_job(name: str) -> dict                            # job info, paused: false, next_run_time set
```

`list_jobs()` entries and all job-info dicts gain `paused` and `cron_expr`.

## Threat model / secret handling

- **Token:** stored job kwargs contain the token from add-time. The update
  path deliberately overwrites it with the current env token (D3.2) — update
  never propagates a stale secret, and never exposes it (return value is
  `_job_info`, which has never contained kwargs).
- **`env_vars` leak:** the pre-existing `add_job` verbatim-logging of
  `env_vars` (`scheduler_service.py:69`) is now **in scope** per user
  decision (2026-09-07) and fixed by D7; all log paths in this feature emit
  env-var names only, never values.
- **No new inputs reach `subprocess`/shell** — `cron_expr` only ever goes
  through `CronTrigger.from_crontab` (parser, not shell).
- **No new network surface**, no compose/port changes; secrets stay where
  they are (env + injected clone URLs, scrubbed by `logging_config`).

## Component changes

| File | Change |
|---|---|
| `app/scheduler_service.py` | `+update_job_schedule()`, `+pause_job()`, `+resume_job()`; `_job_info` gains `paused`/`cron_expr` (D4, name-keyed trigger derivation); `add_job` log event redacted to env-var names only (D7); log events per D3/D5 |
| `app/job_tools.py` | `+update_job_schedule()`, `+pause_job()`, `+resume_job()` wrappers with D5 error mapping |
| `app/server.py` | 3× `mcp.add_tool(...)`; `list_jobs` description updated to mention paused state + schedule |
| `tests/test_scheduler_service.py` | service-level behavior incl. restart persistence (see tasks) |
| `tests/test_job_tools.py` | delegation + error-mapping tests |

No changes: `storage.py`, `executor.py`, `main.py`, compose, Dockerfile.

## Testing design

Mirrors existing patterns exactly:

- **Service tests** (`test_scheduler_service.py` style): real
  `SchedulerService` on `tmp_path` sqlite + `_patch_storage`; assert on
  `_job_info` fields and `scheduler.get_job(name).kwargs`.
- **Tool tests** (`test_job_tools.py` style): `MagicMock(SchedulerService)`
  delegation + exception→error-dict mapping.
- **No new test file** for `server.py` (none exists today; registration is
  verified post-deploy by IaC-DevOps).

## Deployment / rollout

- Direct push to `main` (no PR flow in this repo): engineer → tests green →
  commit + push to `main` → user builds/pushes
  `registry.localdomain:5000/gh-cron-mcp:latest`.
- gh-cron-mcp is an always-on service → **Watchtower** picks the image up;
  no pull-before-run hook applies.
- **No data migration:** paused state rides the jobstore's existing
  `next_run_time` column; `paused`/`cron_expr` are computed, not stored.
- Restart/redeploy is the real-world restart-persistence proof (AC7.2):
  after deploy, pause a job, `docker compose restart gh-cron-mcp`, confirm
  still paused via `list_jobs`.

## Edge cases (decided semantics)

| Case | Behavior |
|---|---|
| Update a paused job | schedule changes, stays paused (atomic via `next_run_time=None` re-add) |
| Pause a mid-run job | in-flight run completes; future fires stop |
| `run_job_now` while paused | allowed (D6) |
| Invalid cron on update | job untouched; clean error (validate before re-add) |
| Unknown name (all 3 tools) | clean error; no job created/removed |
| Resume an active job | idempotent no-op returning job info |
| Restart while paused | still paused; no fire-on-boot (D2) |
