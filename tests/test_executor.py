import subprocess
from pathlib import Path

from app import executor, storage


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


def test_run_job_clones_and_executes(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "REPOS_DIR", tmp_path / "data" / "repos")
    monkeypatch.setattr(storage, "LOGS_DIR", tmp_path / "data" / "logs")
    monkeypatch.setattr(storage, "STATUS_PATH", tmp_path / "data" / "status.json")

    fixture_repo = _init_fixture_repo(tmp_path)

    status = executor.run_job(
        name="test-job",
        repo_url=str(fixture_repo),
        ref="main",
        entrypoint="sh run.sh",
        env_vars=None,
        token=None,
    )

    assert status["success"] is True
    assert status["returncode"] == 0
    log_content = storage.log_path("test-job").read_text()
    assert "hello-cron-test" in log_content


def test_run_job_second_call_pulls_not_reclones(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "REPOS_DIR", tmp_path / "data" / "repos")
    monkeypatch.setattr(storage, "LOGS_DIR", tmp_path / "data" / "logs")
    monkeypatch.setattr(storage, "STATUS_PATH", tmp_path / "data" / "status.json")
    fixture_repo = _init_fixture_repo(tmp_path)

    executor.run_job(name="test-job", repo_url=str(fixture_repo), ref="main", entrypoint="sh run.sh", env_vars=None, token=None)
    status = executor.run_job(name="test-job", repo_url=str(fixture_repo), ref="main", entrypoint="sh run.sh", env_vars=None, token=None)

    assert status["success"] is True
    log_lines = storage.log_path("test-job").read_text().count("hello-cron-test")
    assert log_lines == 2
