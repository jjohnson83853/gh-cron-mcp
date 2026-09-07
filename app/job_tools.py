from typing import Optional

from .executor import JobAlreadyRunning
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
        except JobAlreadyRunning:
            return {"error": f"job {name!r} is already running", "skipped": True}

    def get_job_logs(self, name: str, lines: int = 100) -> str:
        return self._scheduler.get_job_logs(name, lines)

    def update_job_schedule(self, name: str, cron_expr: str) -> dict:
        try:
            return self._scheduler.update_job_schedule(name, cron_expr)
        except JobNotFound:
            return {"error": f"no job named {name!r}"}
        except ValueError as exc:
            return {"error": f"invalid cron_expr {cron_expr!r}: {exc}"}

    def pause_job(self, name: str) -> dict:
        try:
            return self._scheduler.pause_job(name)
        except JobNotFound:
            return {"error": f"no job named {name!r}"}

    def resume_job(self, name: str) -> dict:
        try:
            return self._scheduler.resume_job(name)
        except JobNotFound:
            return {"error": f"no job named {name!r}"}
