# gh-cron-mcp — Schedule Management (Update / Pause / Resume) — Requirements

**Date:** 2026-09-07
**Status:** Confirmed — checkpoint 1 decisions signed off by user, 2026-09-07
**Related:** `docs/superpowers/specs/2026-08-23-gh-cron-mcp-design.md` (product baseline)

## Problem

The current MCP tool surface can create and destroy scheduled jobs, but not
modify or temporarily disable them:

- **Changing a schedule requires `remove_job` + `add_job`.** The LLM must
  re-supply `repo_url`, `entrypoint`, and `ref` — values it may not have in
  context — and `remove_job` deletes last-run status
  (`storage.remove_job_status`), so a schedule change destroys the job's run
  history. A typo'd `repo_url` on re-add silently breaks the job.
- **There is no way to disable a job without deleting it.** `remove_job` is the
  only off-switch, it is destructive, and undoing it requires the full original
  job definition again.

Requested capabilities: update a job's cron schedule using only its name;
pause a job's schedule without deleting it; re-enable it later.

## Should this exist? (PO note)

Yes — checked for simpler paths first:

- APScheduler (already the scheduling engine, with a persistent SQLite
  jobstore) natively supports pause/resume and trigger replacement. The gap
  is purely MCP tool surface; no new service, dependency, or storage schema.
- Without these tools the LLM's only workflow is destructive remove+re-add,
  which loses status and invites re-create-with-wrong-params failures.
- Cost is ~3 small service methods, 3 thin tool wrappers, 3 registrations,
  plus tests — proportionate to the problem.

## Functional requirements

Each requirement has explicit acceptance criteria (AC). "Job info" means the
dict returned by `add_job` / the new tools / entries in `list_jobs`.

### FR1 — Update a job's schedule by name only

`update_job_schedule(name, cron_expr)` changes the cron schedule of an
existing job. The tool signature has **no** `repo_url`, `entrypoint`, `ref`,
or `env_vars` parameters; those values are preserved from the stored job
definition (persisted in the APScheduler jobstore).

- **AC1.1:** After the call, the job's trigger reflects the new `cron_expr`
  and `next_run_time` is recomputed from the new schedule.
- **AC1.2:** `repo_url`/`ref`/`entrypoint`/`env_vars` of the stored job are
  unchanged by the call.
- **AC1.3:** No `repo_url` parameter exists in the tool signature.
- **AC1.4:** Calling it for a non-existent job name returns a clean
  "no job named …" error and does **not** create a job.
- **AC1.5:** An invalid `cron_expr` produces a clean validation error naming
  the bad input; the job is left fully untouched (no partial mutation).

### FR2 — Pause a job without deleting it

`pause_job(name)` stops the job's scheduled fires while keeping the job, its
definition, logs, repo clone, and last-run status intact.

- **AC2.1:** A paused job has no `next_run_time` and will not fire on
  schedule until resumed.
- **AC2.2:** The job still appears in `list_jobs()`.
- **AC2.3:** Pausing does not delete or reset the job's status/logs (unlike
  `remove_job`).
- **AC2.4:** Pausing a mid-run job lets the in-flight run complete; only
  future scheduled fires stop.
- **AC2.5:** Unknown name → clean "no job named …" error.

### FR3 — Resume a paused job

`resume_job(name)` re-enables scheduled fires for a paused job.

- **AC3.1:** After resume, `next_run_time` is recomputed from the job's cron
  trigger (next occurrence after resume time — no catch-up burst of missed
  runs).
- **AC3.2:** Unknown name → clean "no job named …" error.
- **AC3.3:** Resuming an already-active job is harmless (idempotent).

### FR4 — State is observable

The LLM must be able to see schedule and paused state. `list_jobs()` (and the
job-info dicts returned by the new tools) expose:

- `paused` — explicit boolean, not just `next_run_time: null`.
- `cron_expr` — the job's current 5-field cron expression (currently there
  is no way at all to read a job's schedule).

- **AC4.1:** An active job reports `paused: false`; a paused job reports
  `paused: true` with `next_run_time: null`.
- **AC4.2:** `cron_expr` matches the expression the job was created/last
  updated with (normalized cron-field rendering acceptable, e.g. `*/2`).

### FR5 — Clean errors, never tracebacks

- **AC5.1:** Unknown job names on all three tools surface as clean tool
  errors (reusing the existing `JobNotFound` + message pattern), not
  exceptions/tracebacks.
- **AC5.2:** Invalid `cron_expr` surfaces as a clean error naming the input
  and the parse problem.

### FR6 — Update preserves paused state

- **AC6.1:** `update_job_schedule` on a paused job changes the schedule but
  leaves the job paused (no accidental re-enable).

### FR7 — Restart persistence

The service runs as a persistent container (`restart: unless-stopped`, volume
at `/data`); all new state must survive restart/redeploy via the existing
jobstore, with no new persistence mechanism.

- **AC7.1:** An updated schedule survives a scheduler restart.
- **AC7.2:** A paused job is still paused after a scheduler restart (no
  fire-on-boot).
- **AC7.3:** A resumed job keeps its schedule after restart (baseline
  behavior, regression-checked).

### FR8 — Manual runs are orthogonal to paused state

- **AC8.1:** `run_job_now` on a paused job still works — pausing disables
  the *schedule*, not the job.

### FR9 — `env_vars` values are never logged (bundled hardening)

The pre-existing `add_job` log event writes `env_vars` verbatim
(`scheduler_service.py:69`); if those hold credentials they land in
container logs. Per user decision (2026-09-07) this hardening is bundled
into this feature's task plan.

- **AC9.1:** The `add_job` "job added" log event includes env-var **names
  only** (sorted list); values never appear in any log record.
- **AC9.2:** The new log events introduced by this feature
  (`job paused`, `job resumed`, `job schedule updated`) also never contain
  `env_vars` values.
- **AC9.3:** A service test asserts a value-bearing env var does not appear
  in captured logs for `add_job`.

## Non-functional requirements

- **Operational simplicity:** no new services, dependencies, storage schema,
  or status.json fields. Paused state lives where APScheduler already
  persists it (the jobstore) — one source of truth.
- **Convention consistency:** verb-per-tool naming matching the existing
  surface (`add_job`, `remove_job`, …); error-dict shape matching
  `run_job_now`; structured log events matching the existing
  `logger.info("job …")` style.
- **Security:** no new secret surfaces. The update path must use the
  *current* process token (env `GITHUB_TOKEN`), never echo back or re-log
  `env_vars` (which may hold credentials). The pre-existing `add_job`
  env_vars log leak is fixed in this increment (FR9, user-approved scope
  addition).
- **Backward compatibility:** `_job_info` changes are additive; existing
  tool outputs remain valid supersets.

## Non-goals

- No updates to `entrypoint`/`ref`/`env_vars`/`repo_url` via these tools
  (separate increment if ever needed — still possible today via remove+add).
- No pause-cancels-running-execution semantics.
- No new HTTP endpoints, compose changes, or UI.
- No changes to `add_job` semantics (including its current error surface);
  the sole `add_job` change is log redaction of `env_vars` (FR9).
- No catch-up/missed-run replay on resume.

## Constraints

- PR-based delivery: `engineer` implements + tests, `iac-devops` opens the
  PR, user reviews/merges, user builds and pushes
  `registry.localdomain:5000/gh-cron-mcp:latest`.
- gh-cron-mcp is an always-on compose service → image pickup is via the
  existing Watchtower, not a pull-before-run hook.
- Pinned deps: `apscheduler>=3.10,<4` (3.x API), `mcp==2.0.0`.
- Existing test conventions must be mirrored (service tests against a real
  APScheduler + tmp sqlite DB; tool tests via `MagicMock` delegation).

## Confirmed decisions (checkpoint 1 — user, 2026-09-07)

1. **Tool shape:** three dedicated tools — `update_job_schedule(name,
   cron_expr)`, `pause_job(name)`, `resume_job(name)` (over toggle and
   generic-update alternatives).
2. **Scope:** update changes ONLY `cron_expr`; entrypoint/ref/env_vars
   updates stay out of scope.
3. **`run_job_now` on a paused job:** allowed (FR8 stands as written).
4. **`cron_expr` + `paused` in job info:** in scope (FR4 stands as written).
5. **Scope addition:** the `env_vars` credential-leak logging fix is bundled
   into this task plan (FR9).
