# gh-cron-mcp — Schedule Management (Update / Pause / Resume) — Tasks

> **For agentic workers:** Use superpowers:subagent-driven-development (or
> superpowers:executing-plans) to implement task-by-task. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Add `update_job_schedule`, `pause_job`, `resume_job` MCP tools so an
LLM can change a job's cron by name only, and disable/re-enable a job's
schedule without deleting it.

**Spec:** `docs/superpowers/specs/2026-09-07-gh-cron-mcp-schedule-management-requirements.md`
**Design:** `docs/superpowers/specs/2026-09-07-gh-cron-mcp-schedule-management-design.md`
(decisions D1–D6, FRs referenced below by name — read both before starting)

## Global constraints

- APScheduler 3.x API only (`apscheduler>=3.10,<4`, pinned in
  `requirements.txt`); use `scheduler.pause_job`/`resume_job` and
  `add_job(next_run_time=None)` semantics (D2/D3).
- TDD: failing test first in each task; mirror the existing test patterns —
  service tests use `_patch_storage` + real `SchedulerService` on
  `tmp_path` sqlite (`tests/test_scheduler_service.py`), tool tests use
  `MagicMock` delegation (`tests/test_job_tools.py`).
- Error surface: `JobNotFound` → `{"error": "no job named 'x'"}`; cron
  `ValueError` → `{"error": "invalid cron_expr '…': <msg>"}` (D5).
- Never log or return `env_vars`/`token` in the new code paths (threat
  model); `add_job`'s log event is redacted to env-var **names only** per
  D7/FR9 (Task E5).
- No changes outside the files named below; `storage.py`, `executor.py`,
  `main.py`, compose, Dockerfile are untouched.

---

## Engineer tasks

### Task E1 — Service: `pause_job` / `resume_job` + `_job_info` enrichment

**Files:** `app/scheduler_service.py`, `tests/test_scheduler_service.py`

- [ ] Write failing tests (service level, `_patch_storage` pattern):
  - `test_pause_resume_job`: add `nightly` (`0 2 * * *`); `pause_job("nightly")`
    → returned info has `paused: True`, `next_run_time: None`; job still in
    `list_jobs()`; `resume_job("nightly")` → `paused: False`,
    `next_run_time` not `None`. Assert a `"job paused"`/`"job resumed"` log
    record exists (caplog, mirrors `test_missed_job_is_logged` style).
  - `test_pause_job_unknown_name_raises` and
    `test_resume_job_unknown_name_raises`: `pytest.raises(JobNotFound)`.
  - `test_resume_active_job_is_idempotent`: resume without pause → no error,
    `paused: False`, `next_run_time` still set.
  - `test_job_info_exposes_cron_expr_and_paused`: active job →
    `cron_expr == "0 2 * * *"`, `paused is False` (AC4.1/AC4.2).
  - `test_paused_state_survives_restart`: add → pause → `shutdown()`; new
    `SchedulerService(db_path=…)` → `list_jobs()` shows `paused: True` and
    `next_run_time: None`; resume on the new instance works (AC7.2).
- [ ] Implement in `SchedulerService`:
  - `pause_job(name)` / `resume_job(name)`: `get_job` None-check →
    `JobNotFound`; delegate to `self._scheduler.pause_job/resume_job`;
    log `"job paused"`/`"job resumed"` with `{job}`; return `self._job_info(name)`.
  - `_job_info`: add `"paused": job.next_run_time is None` and
    `"cron_expr"` derived from the trigger fields **by name**
    (`minute, hour, day, month, day_of_week` — join `str(field)`, defaulting
    missing to `"*"`), per D4.
- [ ] Verify: all new tests green; existing suite unaffected.

**Acceptance:** FR2 (AC2.1–AC2.5), FR3 (AC3.1–AC3.3), FR4 (AC4.1–AC4.2),
FR7 (AC7.2).

### Task E2 — Service: `update_job_schedule`

**Files:** `app/scheduler_service.py`, `tests/test_scheduler_service.py`

- [ ] Write failing tests:
  - `test_update_job_schedule_changes_trigger_in_place`: add `nightly`
    (`0 2 * * *`, `entrypoint="true"`, distinctive `repo_url`/`ref`/
    `env_vars`); update to `30 3 * * *` → info `cron_expr == "30 3 * * *"`,
    `next_run_time` not `None`; `self._scheduler.get_job("nightly").kwargs`
    still carries the original `repo_url`/`ref`/`entrypoint`/`env_vars`
    (AC1.1, AC1.2).
  - `test_update_job_schedule_unknown_name_raises`: `JobNotFound`; then
    `get_job(name) is None` — no job was created (AC1.4).
  - `test_update_job_schedule_invalid_cron_raises`: `ValueError`; job's
    stored trigger/kwargs unchanged afterwards (AC1.5).
  - `test_update_job_schedule_preserves_paused_state`: add → pause → update →
    info shows `paused: True`, `next_run_time: None`; resume → next fire per
    the NEW expression (AC6.1).
  - `test_updated_schedule_survives_restart`: add `0 2 * * *` → update
    `30 3 * * *` → shutdown → new service → `cron_expr == "30 3 * * *"`
    (AC7.1).
  - `test_run_job_now_works_on_paused_job`: pause, then `run_job_now` on a
    local fixture repo (mirror `tests/test_executor.py`'s `_init_fixture_repo`
    local-git pattern, `entrypoint="true"`) → returns a status dict, no
    exception (AC8.1, D6).
- [ ] Implement `update_job_schedule(name, cron_expr)` per D3, in order:
  `get_job` check → read identity kwargs (drop stored `token`, substitute
  `self._token`) → capture `was_paused` → `CronTrigger.from_crontab`
  validation → re-add via the existing `replace_existing=True` path, passing
  `next_run_time=None` when `was_paused` → log `"job schedule updated"` with
  `{job, cron_expr, paused}` only → return `_job_info(name)`.
  If the `next_run_time=None`-on-re-add assumption fails (design's labeled
  fallback): re-add actively, then call `pause_job(name)` immediately, and
  note the deviation in the PR.
- [ ] Verify: all tests green.

**Acceptance:** FR1 (AC1.1–AC1.5), FR6 (AC6.1), FR7 (AC7.1), FR8 (AC8.1).

### Task E3 — Tools: `JobTools` wrappers with error mapping

**Files:** `app/job_tools.py`, `tests/test_job_tools.py`

- [ ] Write failing tests (MagicMock delegation pattern):
  - `test_update_job_schedule_delegates_to_scheduler` — called with
    `(name, cron_expr)`; passthrough result.
  - `test_update_job_schedule_handles_not_found` — `JobNotFound` side effect
    → `{"error": "no job named 'ghost'"}`.
  - `test_update_job_schedule_handles_invalid_cron` — `ValueError` side
    effect → error dict starting `"invalid cron_expr"`.
  - `test_pause_job_delegates` / `test_pause_job_handles_not_found`.
  - `test_resume_job_delegates` / `test_resume_job_handles_not_found`.
- [ ] Implement the three wrappers per D5 (`JobNotFound`/`ValueError` →
  error dicts; success → scheduler's job-info dict).
- [ ] Verify: tests green.

**Acceptance:** FR5 (AC5.1–AC5.2) at the tool boundary; FR1–FR3 tool
signatures have no `repo_url` param (AC1.3 — assert in delegation tests by
inspecting `JobTools.update_job_schedule.__code__.co_varnames` or simply by
the delegation call shape).

### Task E4 — Registration + full-suite verification

**Files:** `app/server.py`

- [ ] Register in `build_server` (D1), with LLM-clear descriptions:
  - `update_job_schedule` — "Update an existing job's cron schedule by name.
    Only the schedule changes; repo, entrypoint, and env stay as-is."
  - `pause_job` — "Pause a job's schedule without deleting the job or its
    history. Manual run_job_now still works while paused."
  - `resume_job` — "Resume a paused job's schedule."
  - Update `list_jobs` description to mention schedule + paused state.
- [ ] Run the complete suite: `pytest tests/` — all green, no skips introduced.

**Acceptance:** MCP surface complete per D1; no regressions.

### Task E5 — Hardening: redact `env_vars` in the `add_job` log event (D7, FR9)

**Files:** `app/scheduler_service.py`, `tests/test_scheduler_service.py`

- [ ] Write failing test (service level, `_patch_storage` + caplog):
  `test_add_job_log_does_not_leak_env_vars`: `add_job("leakcheck", …,
  env_vars={"SECRET_TOKEN": "hunter2", "REGION": "us-east"})` → the captured
  `"job added"` log record contains `"SECRET_TOKEN"` and `"REGION"` (names)
  but NOT `"hunter2"` (value); the record's env_vars rendering is the sorted
  name list (AC9.1, AC9.3).
- [ ] Implement in `add_job`: replace the verbatim `env_vars` log field with
  `sorted(env_vars)` names only (D7 — names list, values never serialized).
- [ ] Verify: test green; grep the test log capture for the value string to
  prove absence (mechanical check, not eyeball).

**Acceptance:** FR9 (AC9.1–AC9.3). Log-only change — no behavior/API surface
touched.

---

## IaC-DevOps tasks

### Task D1 — PR + deploy

- [ ] Open PR containing E1–E5 changes (code + tests + no doc drift); link
  both spec docs. User reviews and merges.
- [ ] User builds and pushes
  `registry.localdomain:5000/gh-cron-mcp:latest` (standard local build; no
  CI — per environment rules).
- [ ] Watchtower picks the image up (always-on service; no pull hook needed).
  Confirm redeploy via `curl http://<host>:8000/health` + container start
  time.

### Task D2 — Post-deploy verification (production restart persistence)

- [ ] `list_jobs` → pick a low-stakes job; record its `cron_expr`.
- [ ] `pause_job` it → confirm `paused: true`, `next_run_time: null`.
- [ ] `docker compose restart gh-cron-mcp` (or wait for Watchtower redeploy
  if timed together).
- [ ] After restart: `list_jobs` → job still present, still `paused: true`
  (AC7.2, real-world).
- [ ] `update_job_schedule` on that job to a throwaway expression → confirm
  `cron_expr` changed and `paused: true` retained (AC6.1).
- [ ] `resume_job` → `paused: false`, `next_run_time` per the new schedule;
  `update_job_schedule` back to the original expression.
- [ ] Verify unknown-name errors come back as clean messages, not tracebacks
  (AC5.1), and bad-cron likewise (AC5.2).
- [ ] Confirm `/data/scheduler.db` needed no migration (no schema error in
  logs at startup).

**Acceptance:** all FRs verified against the deployed service.
