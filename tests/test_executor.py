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


def test_run_job_logs_commit_and_run_id(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(storage, "REPOS_DIR", tmp_path / "data" / "repos")
    monkeypatch.setattr(storage, "LOGS_DIR", tmp_path / "data" / "logs")
    monkeypatch.setattr(storage, "STATUS_PATH", tmp_path / "data" / "status.json")
    fixture_repo = _init_fixture_repo(tmp_path)

    with caplog.at_level("INFO", logger="gh_cron_mcp"):
        executor.run_job(name="test-job", repo_url=str(fixture_repo), ref="main", entrypoint="sh run.sh", env_vars=None, token=None)

    finished = [r for r in caplog.records if r.message == "job finished"][0]
    assert finished.extra_fields["commit"]
    assert finished.extra_fields["run_id"]


def test_run_job_surfaces_failure_output_inline(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(storage, "REPOS_DIR", tmp_path / "data" / "repos")
    monkeypatch.setattr(storage, "LOGS_DIR", tmp_path / "data" / "logs")
    monkeypatch.setattr(storage, "STATUS_PATH", tmp_path / "data" / "status.json")
    fixture_repo = _init_fixture_repo(tmp_path)

    with caplog.at_level("ERROR", logger="gh_cron_mcp"):
        status = executor.run_job(
            name="test-job", repo_url=str(fixture_repo), ref="main",
            entrypoint="echo boom-message >&2; exit 1", env_vars=None, token=None,
        )

    assert status["success"] is False
    finished = [r for r in caplog.records if r.message == "job finished"][0]
    assert "boom-message" in finished.extra_fields["output_tail"]
    assert finished.extra_fields["output_truncated"] is False


def test_run_job_scrubs_credentials_from_git_failure(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(storage, "REPOS_DIR", tmp_path / "data" / "repos")
    monkeypatch.setattr(storage, "LOGS_DIR", tmp_path / "data" / "logs")
    monkeypatch.setattr(storage, "STATUS_PATH", tmp_path / "data" / "status.json")

    # git itself echoes the credentialed clone URL back in its own fatal
    # errors — simulate that instead of hitting a real remote.
    def _fake_sync_repo(repo_url, ref, dest, token):
        raise subprocess.CalledProcessError(
            128, ["git", "clone"],
            stderr="fatal: unable to access 'https://x-access-token:ghp_supersecrettoken@github.com/x/y.git/': "
                   "The requested URL returned error: 404",
        )

    monkeypatch.setattr(executor, "sync_repo", _fake_sync_repo)

    with caplog.at_level("ERROR", logger="gh_cron_mcp"):
        status = executor.run_job(
            name="test-job", repo_url="https://github.com/x/y.git", ref="main",
            entrypoint="true", env_vars=None, token="ghp_supersecrettoken",
        )

    assert status["success"] is False
    assert "ghp_supersecrettoken" not in status["error"]
    log_content = storage.log_path("test-job").read_text()
    assert "ghp_supersecrettoken" not in log_content
    failed = [r for r in caplog.records if r.message == "job git sync failed"][0]
    assert "ghp_supersecrettoken" not in failed.extra_fields["stderr"]
