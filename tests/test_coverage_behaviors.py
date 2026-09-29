import pytest

import subprocess

from mcp.server.mcpserver import MCPServer

from app import executor, main, server, storage


def test_main_builds_server_and_runs_http_transport(monkeypatch):
    events = []

    class Scheduler:
        def __init__(self, github_token=None):
            events.append(("scheduler", github_token))

    def run_server(self, **kwargs):
        events.append(("run", kwargs))

    monkeypatch.setattr(main, "SchedulerService", Scheduler)
    monkeypatch.setattr(MCPServer, "run", run_server)
    monkeypatch.setenv("GITHUB_TOKEN", "offline-token")
    monkeypatch.setenv("PORT", "9123")
    main.main()
    assert events == [
        ("scheduler", "offline-token"),
        ("run", {"transport": "streamable-http", "stateless_http": True,
                 "json_response": True, "host": "0.0.0.0", "port": 9123}),
    ]


def test_storage_rotates_logs_and_preserves_status(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(storage, "REPOS_DIR", data_dir / "repos")
    monkeypatch.setattr(storage, "LOGS_DIR", data_dir / "logs")
    monkeypatch.setattr(storage, "STATUS_PATH", data_dir / "status.json")
    monkeypatch.setattr(storage, "LOG_CAP_BYTES", 16)
    storage.ensure_dirs()
    assert storage.repo_path("job") == data_dir / "repos" / "job"

    log = storage.log_path("job")
    log.write_text("x" * 20)
    storage.append_log("job", "tail")
    assert log.read_text().endswith("x" * 4 + "tail")
    assert log.stat().st_size < 30

    assert storage.read_status() == {}
    storage.write_job_status("job", {"success": True})
    storage.write_job_status("other", {"success": False})
    assert storage.read_status() == {
        "job": {"success": True}, "other": {"success": False},
    }
    storage.remove_job_status("job")
    storage.remove_job_status("missing")
    assert storage.read_status() == {"other": {"success": False}}


def test_sync_repo_clones_then_updates_existing_checkout(tmp_path, monkeypatch):
    dest = tmp_path / "repos" / "job"
    commands = []

    def successful_run(args, **kwargs):
        commands.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(executor.subprocess, "run", successful_run)
    executor.sync_repo("https://github.com/o/r.git", "stable", dest, "token")
    assert commands[0][0] == [
        "git", "clone", "--branch", "stable", "--depth", "1",
        "https://x-access-token:token@github.com/o/r.git", str(dest),
    ]
    assert commands[0][1]["check"] is True
    (dest / ".git").mkdir(parents=True)
    commands.clear()
    executor.sync_repo("https://github.com/o/r.git", "stable", dest, None)
    assert [cmd[0][1] for cmd in commands] == ["remote", "fetch", "checkout", "reset"]
    assert all(kwargs["check"] is True for _, kwargs in commands)
    assert commands[-1][0][-1] == "origin/stable"

    def head_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")

    monkeypatch.setattr(executor.subprocess, "run", head_run)
    assert executor._head_commit(dest) == "abc123"

    def failed_run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="failed")

    monkeypatch.setattr(executor.subprocess, "run", failed_run)
    with pytest.raises(subprocess.CalledProcessError):
        executor.sync_repo("https://github.com/o/r.git", "stable", tmp_path / "failed", None)


def test_docker_cleanup_handles_unavailable_host_and_failed_commands(monkeypatch):
    def failed_ps(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, stdout="", stderr="denied")

    monkeypatch.setattr(executor.subprocess, "run", failed_ps)
    assert executor._docker_ps_ids({}) is None
    assert executor._reap_orphaned_containers(set(), {}) is None

    calls = []

    def cleanup_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[1] == "stop" else 0, stderr="bad")

    monkeypatch.setattr(executor.subprocess, "run", cleanup_run)
    assert executor._stop_and_remove("orphan", {}) is False
    assert [cmd[1] for cmd in calls] == ["stop", "rm"]
    assert executor._tail("x" * 2001) == ("x" * 2000, True)
    assert executor._tail("") == ("", False)
