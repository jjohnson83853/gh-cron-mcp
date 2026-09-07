import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, JobExecutionEvent

from app import storage
from app.scheduler_service import JobNotFound, SchedulerService


def _patch_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "REPOS_DIR", tmp_path / "repos")
    monkeypatch.setattr(storage, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(storage, "STATUS_PATH", tmp_path / "status.json")


def _init_fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fixture_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "run.sh").write_text("#!/bin/sh\necho hello-cron-test\n")
    subprocess.run(["git", "add", "run.sh"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def test_update_job_schedule_changes_trigger_in_place(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))
    svc.add_job(
        name="nightly", repo_url="https://example.invalid/original-repo.git",
        ref="develop", entrypoint="make run", cron_expr="0 2 * * *",
        env_vars={"REGION": "us-east"},
    )

    info = svc.update_job_schedule("nightly", "30 3 * * *")

    assert info["cron_expr"] == "30 3 * * *"
    assert info["next_run_time"] is not None
    kwargs = svc._scheduler.get_job("nightly").kwargs
    assert kwargs["repo_url"] == "https://example.invalid/original-repo.git"
    assert kwargs["ref"] == "develop"
    assert kwargs["entrypoint"] == "make run"
    assert kwargs["env_vars"] == {"REGION": "us-east"}
    svc.shutdown()


def test_update_job_schedule_unknown_name_raises(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))
    with pytest.raises(JobNotFound):
        svc.update_job_schedule("ghost", "0 2 * * *")
    assert svc._scheduler.get_job("ghost") is None
    svc.shutdown()


def test_update_job_schedule_invalid_cron_raises(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))
    svc.add_job(
        name="nightly", repo_url="https://example.invalid/repo.git", ref="main",
        entrypoint="true", cron_expr="0 2 * * *",
    )
    kwargs_before = dict(svc._scheduler.get_job("nightly").kwargs)

    with pytest.raises(ValueError):
        svc.update_job_schedule("nightly", "not a cron")

    info = svc._job_info("nightly")
    assert info["cron_expr"] == "0 2 * * *"
    assert svc._scheduler.get_job("nightly").kwargs == kwargs_before
    svc.shutdown()


def test_update_job_schedule_preserves_paused_state(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))
    svc.add_job(
        name="nightly", repo_url="https://example.invalid/repo.git", ref="main",
        entrypoint="true", cron_expr="0 2 * * *",
    )
    svc.pause_job("nightly")

    info = svc.update_job_schedule("nightly", "30 3 * * *")

    assert info["paused"] is True
    assert info["next_run_time"] is None

    resumed = svc.resume_job("nightly")
    assert resumed["next_run_time"] is not None
    next_fire = datetime.fromisoformat(resumed["next_run_time"])
    # resume recomputes from the NEW expression, not the original one (AC6.1)
    assert (next_fire.minute, next_fire.hour) == (30, 3)
    svc.shutdown()


def test_updated_schedule_survives_restart(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    db_path = str(tmp_path / "scheduler.db")

    svc = SchedulerService(db_path=db_path)
    svc.add_job(
        name="nightly", repo_url="https://example.invalid/repo.git", ref="main",
        entrypoint="true", cron_expr="0 2 * * *",
    )
    svc.update_job_schedule("nightly", "30 3 * * *")
    svc.shutdown()

    svc2 = SchedulerService(db_path=db_path)
    jobs = svc2.list_jobs()
    assert jobs[0]["cron_expr"] == "30 3 * * *"
    svc2.shutdown()


def test_run_job_now_works_on_paused_job(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    fixture_repo = _init_fixture_repo(tmp_path)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))
    svc.add_job(
        name="nightly", repo_url=str(fixture_repo), ref="main",
        entrypoint="true", cron_expr="0 2 * * *",
    )
    svc.pause_job("nightly")

    status = svc.run_job_now("nightly")

    assert isinstance(status, dict)
    assert status["success"] is True
    svc.shutdown()


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


def test_pause_resume_job(tmp_path, monkeypatch, caplog):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))
    svc.add_job(
        name="nightly", repo_url="https://example.invalid/repo.git", ref="main",
        entrypoint="true", cron_expr="0 2 * * *",
    )

    with caplog.at_level("INFO", logger="gh_cron_mcp"):
        info = svc.pause_job("nightly")
        assert info["paused"] is True
        assert info["next_run_time"] is None
        assert "nightly" in [j["name"] for j in svc.list_jobs()]

        info = svc.resume_job("nightly")
        assert info["paused"] is False
        assert info["next_run_time"] is not None

    paused = [r for r in caplog.records if "paused" in r.message][0]
    assert paused.extra_fields["job"] == "nightly"
    resumed = [r for r in caplog.records if "resumed" in r.message][0]
    assert resumed.extra_fields["job"] == "nightly"
    svc.shutdown()


def test_pause_job_unknown_name_raises(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))
    with pytest.raises(JobNotFound):
        svc.pause_job("does-not-exist")
    svc.shutdown()


def test_resume_job_unknown_name_raises(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))
    with pytest.raises(JobNotFound):
        svc.resume_job("does-not-exist")
    svc.shutdown()


def test_resume_active_job_is_idempotent(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))
    svc.add_job(
        name="nightly", repo_url="https://example.invalid/repo.git", ref="main",
        entrypoint="true", cron_expr="0 2 * * *",
    )

    info = svc.resume_job("nightly")

    assert info["paused"] is False
    assert info["next_run_time"] is not None
    svc.shutdown()


def test_job_info_exposes_cron_expr_and_paused(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))

    info = svc.add_job(
        name="nightly", repo_url="https://example.invalid/repo.git", ref="main",
        entrypoint="true", cron_expr="0 2 * * *",
    )

    assert info["cron_expr"] == "0 2 * * *"
    assert info["paused"] is False
    svc.shutdown()


def test_paused_state_survives_restart(tmp_path, monkeypatch):
    _patch_storage(tmp_path, monkeypatch)
    db_path = str(tmp_path / "scheduler.db")

    svc = SchedulerService(db_path=db_path)
    svc.add_job(
        name="nightly", repo_url="https://example.invalid/repo.git", ref="main",
        entrypoint="true", cron_expr="0 2 * * *",
    )
    svc.pause_job("nightly")
    svc.shutdown()

    svc2 = SchedulerService(db_path=db_path)
    jobs = svc2.list_jobs()
    assert [j["name"] for j in jobs] == ["nightly"]
    assert jobs[0]["paused"] is True
    assert jobs[0]["next_run_time"] is None

    info = svc2.resume_job("nightly")
    assert info["paused"] is False
    assert info["next_run_time"] is not None
    svc2.shutdown()


def test_add_job_log_does_not_leak_env_vars(tmp_path, monkeypatch, caplog):
    _patch_storage(tmp_path, monkeypatch)
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"))

    with caplog.at_level("INFO", logger="gh_cron_mcp"):
        svc.add_job(
            name="leakcheck", repo_url="https://example.invalid/repo.git", ref="main",
            entrypoint="true", cron_expr="0 2 * * *",
            env_vars={"SECRET_TOKEN": "hunter2", "REGION": "us-east"},
        )

    records = [r for r in caplog.records if "scheduled" in r.message]
    assert records, "add_job log record not captured"
    record = records[0]
    # names present, sorted (AC9.1)
    assert record.extra_fields["env_vars"] == ["REGION", "SECRET_TOKEN"]
    # values never serialized into any captured log record (AC9.3)
    captured = " ".join(
        f"{r.message} {r.extra_fields}" for r in caplog.records if hasattr(r, "extra_fields")
    )
    assert "hunter2" not in captured
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
