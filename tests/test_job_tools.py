from unittest.mock import MagicMock

from app.executor import JobAlreadyRunning
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


def test_run_job_now_handles_already_running():
    scheduler = MagicMock()
    scheduler.run_job_now.side_effect = JobAlreadyRunning("nightly")
    tools = JobTools(scheduler)

    result = tools.run_job_now("nightly")

    assert result == {"error": "job 'nightly' is already running", "skipped": True}
