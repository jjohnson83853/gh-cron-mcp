import logging
from typing import Optional

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import executor, storage

logger = logging.getLogger("gh_cron_mcp")


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
        # A missed cron fire (container down/redeploying at the scheduled time)
        # otherwise produces zero log lines anywhere — the exact 2am failure
        # that leaves no evidence. EVENT_JOB_ERROR also catches anything that
        # escapes executor.run_job's own try/except (there shouldn't be any,
        # but this is the last line of defense before it's silently dropped).
        self._scheduler.add_listener(self._on_job_event, EVENT_JOB_MISSED | EVENT_JOB_ERROR)
        self._scheduler.start()

    def _on_job_event(self, event: JobExecutionEvent) -> None:
        fields = {
            "job": event.job_id,
            "scheduled_run_time": event.scheduled_run_time.isoformat() if event.scheduled_run_time else None,
        }
        if event.code == EVENT_JOB_MISSED:
            logger.error("job misfired (scheduled run did not execute)", extra={"extra_fields": fields})
        else:
            # event.traceback is already a formatted string, not a traceback
            # object, so it goes in extra_fields rather than exc_info.
            fields["exception"] = str(event.exception)
            fields["traceback"] = event.traceback
            logger.error("job raised outside its own error handling", extra={"extra_fields": fields})

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
        logger.info(
            "job scheduled",
            extra={"extra_fields": {
                "job": name, "repo_url": repo_url, "ref": ref,
                "entrypoint": entrypoint, "cron_expr": cron_expr,
                # env-var NAMES only — values may hold credentials and are
                # never serialized to logs (D7/FR9).
                "env_vars": sorted(env_vars) if env_vars else [],
            }},
        )
        return self._job_info(name)

    def remove_job(self, name: str) -> None:
        job = self._scheduler.get_job(name)
        if job is None:
            raise JobNotFound(name)
        self._scheduler.remove_job(name)
        storage.remove_job_status(name)
        logger.info("job removed", extra={"extra_fields": {"job": name}})

    def list_jobs(self) -> list:
        return [self._job_info(job.id) for job in self._scheduler.get_jobs()]

    def pause_job(self, name: str) -> dict:
        job = self._scheduler.get_job(name)
        if job is None:
            raise JobNotFound(name)
        self._scheduler.pause_job(name)
        logger.info("job paused", extra={"extra_fields": {"job": name}})
        return self._job_info(name)

    def resume_job(self, name: str) -> dict:
        job = self._scheduler.get_job(name)
        if job is None:
            raise JobNotFound(name)
        self._scheduler.resume_job(name)
        logger.info("job resumed", extra={"extra_fields": {"job": name}})
        return self._job_info(name)

    def update_job_schedule(self, name: str, cron_expr: str) -> dict:
        job = self._scheduler.get_job(name)
        if job is None:
            raise JobNotFound(name)
        # Never reuse the stored token — substitute the current process token
        # so a rotated GITHUB_TOKEN takes effect on the next run (D3.2).
        kwargs = {
            "name": name,
            "repo_url": job.kwargs["repo_url"],
            "ref": job.kwargs["ref"],
            "entrypoint": job.kwargs["entrypoint"],
            "env_vars": job.kwargs["env_vars"],
            "token": self._token,
        }
        was_paused = job.next_run_time is None
        # Validate before any mutation, so an invalid expression leaves the
        # job fully untouched (D3.4) — ValueError propagates to the caller.
        trigger = CronTrigger.from_crontab(cron_expr)
        # A paused job must land paused atomically: re-add with
        # next_run_time=None (D3.5). When active, omit the parameter so the
        # scheduler computes the next run from the new trigger.
        paused_kwargs = {"next_run_time": None} if was_paused else {}
        self._scheduler.add_job(
            executor.run_job,
            trigger=trigger,
            id=name,
            replace_existing=True,
            max_instances=1,
            kwargs=kwargs,
            **paused_kwargs,
        )
        logger.info(
            "job schedule updated",
            extra={"extra_fields": {"job": name, "cron_expr": cron_expr, "paused": was_paused}},
        )
        return self._job_info(name)

    def run_job_now(self, name: str) -> dict:
        job = self._scheduler.get_job(name)
        if job is None:
            raise JobNotFound(name)
        logger.info("job manually triggered", extra={"extra_fields": {"job": name}})
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
        # Derive the cron expression from the trigger fields BY NAME, never by
        # list position — APScheduler's internal field ordering is not a
        # contract (D4). Missing fields render as "*".
        trigger_fields = {field.name: str(field) for field in job.trigger.fields}
        cron_fields = [trigger_fields.get(name, "*") for name in
                       ("minute", "hour", "day", "month", "day_of_week")]
        return {
            "name": job.id,
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            "last_run": status.get("last_run"),
            "success": status.get("success"),
            "paused": job.next_run_time is None,
            "cron_expr": " ".join(cron_fields),
        }
