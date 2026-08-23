import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import storage


def _clone_url_with_token(repo_url: str, token: Optional[str]) -> str:
    if not token or not repo_url.startswith("https://"):
        return repo_url
    return repo_url.replace("https://", f"https://x-access-token:{token}@", 1)


def sync_repo(repo_url: str, ref: str, dest: Path, token: Optional[str]) -> None:
    auth_url = _clone_url_with_token(repo_url, token)
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--branch", ref, "--depth", "1", auth_url, str(dest)],
            check=True, capture_output=True, text=True,
        )
    else:
        subprocess.run(["git", "remote", "set-url", "origin", auth_url], cwd=dest, check=True, capture_output=True, text=True)
        subprocess.run(["git", "fetch", "--depth", "1", "origin", ref], cwd=dest, check=True, capture_output=True, text=True)
        subprocess.run(["git", "checkout", ref], cwd=dest, check=True, capture_output=True, text=True)
        subprocess.run(["git", "reset", "--hard", f"origin/{ref}"], cwd=dest, check=True, capture_output=True, text=True)


def run_job(
    name: str,
    repo_url: str,
    ref: str,
    entrypoint: str,
    env_vars: Optional[dict],
    token: Optional[str],
    timeout: int = 1800,
) -> dict:
    storage.ensure_dirs()
    dest = storage.repo_path(name)
    log_file = storage.log_path(name)
    started = datetime.now(timezone.utc).isoformat()
    env = {**os.environ, **(env_vars or {})}

    try:
        sync_repo(repo_url, ref, dest, token)
        result = subprocess.run(
            entrypoint, shell=True, cwd=dest, env=env,
            capture_output=True, text=True, timeout=timeout,
        )
        with open(log_file, "a") as f:
            f.write(f"\n=== run {started} ===\n")
            f.write(result.stdout)
            f.write(result.stderr)
        status = {
            "last_run": started,
            "success": result.returncode == 0,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        status = {"last_run": started, "success": False, "returncode": None, "error": "timeout"}
        with open(log_file, "a") as f:
            f.write(f"\n=== run {started} TIMED OUT after {timeout}s ===\n")
    except subprocess.CalledProcessError as e:
        status = {"last_run": started, "success": False, "returncode": e.returncode, "error": e.stderr}
        with open(log_file, "a") as f:
            f.write(f"\n=== run {started} git sync failed ===\n{e.stderr}\n")

    storage.write_job_status(name, status)
    return status
