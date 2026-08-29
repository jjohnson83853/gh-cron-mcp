from datetime import datetime, timezone

import pytest
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, JobExecutionEvent

from app import storage
from app.scheduler_service import JobNotFound, SchedulerService


def _patch_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "REPOS_DIR", tmp_path / "repos")
    monkeypatch.setattr(storage, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(storage, "STATUS_PATH", tmp_path / "status.json")


def test_add_job_persists_across_restart(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    db_path = str(tmp_path / "scheduler.db")

    svc = SchedulerService(db_path=db_path)
    info = svc.add_job(
        name="nightly", repo_url="https://example.invalid/repo.git", ref="main",
        entrypoint="true", cron_expr="0 2 * * *",
    )
    assert info["name"] == "nightly"
    assert info["next_run_time"] is not None
    svc.shutdown()

    svc2 = SchedulerService(db_path=db_path)
    jobs = svc2.list_jobs()
    assert [j["name"] for j in jobs] == ["nightly"]
    svc2.shutdown()


def test_remove_job_raises_job_not_found(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))
    with pytest.raises(JobNotFound):
        svc.remove_job("does-not-exist")
    svc.shutdown()


def test_invalid_cron_expression_raises(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))
    with pytest.raises(ValueError):
        svc.add_job(name="bad", repo_url="https://x/y.git", ref="main", entrypoint="true", cron_expr="not a cron")


def test_missed_job_is_logged(tmp_path, monkeypatch, caplog):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))
    event = JobExecutionEvent(
        code=EVENT_JOB_MISSED, job_id="nightly", jobstore="default",
        scheduled_run_time=datetime(2026, 8, 30, 2, 0, tzinfo=timezone.utc),
    )

    with caplog.at_level("ERROR", logger="gh_cron_mcp"):
        svc._on_job_event(event)

    record = [r for r in caplog.records if "misfired" in r.message][0]
    assert record.extra_fields["job"] == "nightly"
    svc.shutdown()


def test_job_error_is_logged_with_traceback(tmp_path, monkeypatch, caplog):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))
    event = JobExecutionEvent(
        code=EVENT_JOB_ERROR, job_id="nightly", jobstore="default",
        scheduled_run_time=datetime(2026, 8, 30, 2, 0, tzinfo=timezone.utc),
        exception=ValueError("boom"), traceback="Traceback (most recent call last):\nValueError: boom",
    )

    with caplog.at_level("ERROR", logger="gh_cron_mcp"):
        svc._on_job_event(event)

    record = [r for r in caplog.records if "outside its own error handling" in r.message][0]
    assert record.extra_fields["exception"] == "boom"
    assert "Traceback" in record.extra_fields["traceback"]
    svc.shutdown()
