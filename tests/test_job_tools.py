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


def test_update_job_schedule_delegates_to_scheduler():
    scheduler = MagicMock()
    scheduler.update_job_schedule.return_value = {"name": "nightly", "cron_expr": "30 3 * * *", "paused": False}
    tools = JobTools(scheduler)

    result = tools.update_job_schedule(name="nightly", cron_expr="30 3 * * *")

    scheduler.update_job_schedule.assert_called_once_with("nightly", "30 3 * * *")
    assert result == {"name": "nightly", "cron_expr": "30 3 * * *", "paused": False}
    # AC1.3: the tool signature carries the job name and schedule only —
    # repo_url (and the other stored job-definition params) must not exist.
    assert "repo_url" not in JobTools.update_job_schedule.__code__.co_varnames


def test_update_job_schedule_handles_not_found():
    scheduler = MagicMock()
    scheduler.update_job_schedule.side_effect = JobNotFound("ghost")
    tools = JobTools(scheduler)

    result = tools.update_job_schedule("ghost", "0 2 * * *")

    assert result == {"error": "no job named 'ghost'"}


def test_update_job_schedule_handles_invalid_cron():
    scheduler = MagicMock()
    scheduler.update_job_schedule.side_effect = ValueError("bad hour value '99'")
    tools = JobTools(scheduler)

    result = tools.update_job_schedule("nightly", "99 99 * * *")

    assert result == {"error": "invalid cron_expr '99 99 * * *': bad hour value '99'"}


def test_pause_job_delegates():
    scheduler = MagicMock()
    scheduler.pause_job.return_value = {"name": "nightly", "paused": True, "next_run_time": None}
    tools = JobTools(scheduler)

    result = tools.pause_job("nightly")

    scheduler.pause_job.assert_called_once_with("nightly")
    assert result == {"name": "nightly", "paused": True, "next_run_time": None}
    assert "repo_url" not in JobTools.pause_job.__code__.co_varnames


def test_pause_job_handles_not_found():
    scheduler = MagicMock()
    scheduler.pause_job.side_effect = JobNotFound("ghost")
    tools = JobTools(scheduler)

    result = tools.pause_job("ghost")

    assert result == {"error": "no job named 'ghost'"}


def test_resume_job_delegates():
    scheduler = MagicMock()
    scheduler.resume_job.return_value = {"name": "nightly", "paused": False, "next_run_time": "2026-09-08T02:00:00+00:00"}
    tools = JobTools(scheduler)

    result = tools.resume_job("nightly")

    scheduler.resume_job.assert_called_once_with("nightly")
    assert result == {"name": "nightly", "paused": False, "next_run_time": "2026-09-08T02:00:00+00:00"}
    assert "repo_url" not in JobTools.resume_job.__code__.co_varnames


def test_resume_job_handles_not_found():
    scheduler = MagicMock()
    scheduler.resume_job.side_effect = JobNotFound("ghost")
    tools = JobTools(scheduler)

    result = tools.resume_job("ghost")

    assert result == {"error": "no job named 'ghost'"}
