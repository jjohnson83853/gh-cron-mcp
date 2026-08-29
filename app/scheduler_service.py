import logging
from typing import Optional

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
        logger.info(
            "job scheduled",
            extra={"extra_fields": {
                "job": name, "repo_url": repo_url, "ref": ref,
                "entrypoint": entrypoint, "cron_expr": cron_expr,
                "env_vars": env_vars or {},
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
        return {
            "name": job.id,
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            "last_run": status.get("last_run"),
            "success": status.get("success"),
        }
