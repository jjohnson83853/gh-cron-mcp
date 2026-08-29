import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import storage
from .logging_config import scrub_credentials

logger = logging.getLogger("gh_cron_mcp")

_TAIL_CHARS = 2000


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


def _head_commit(dest: Path) -> Optional[str]:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=dest, capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _tail(text: str) -> tuple[str, bool]:
    text = text or ""
    if len(text) <= _TAIL_CHARS:
        return text, False
    return text[-_TAIL_CHARS:], True


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
    started = datetime.now(timezone.utc).isoformat()
    env = {**os.environ, **(env_vars or {})}
    job_fields = {"job": name, "repo_url": repo_url, "ref": ref, "entrypoint": entrypoint, "run_id": started}
    docker_host = (env_vars or {}).get("DOCKER_HOST")
    if docker_host:
        job_fields["docker_host"] = docker_host
    start_time = time.monotonic()

    logger.info("job started", extra={"extra_fields": job_fields})

    try:
        sync_repo(repo_url, ref, dest, token)
        job_fields["commit"] = _head_commit(dest)
        result = subprocess.run(
            entrypoint, shell=True, cwd=dest, env=env,
            capture_output=True, text=True, timeout=timeout,
        )
        storage.append_log(
            name,
            scrub_credentials(f"\n=== run {started} ===\n{result.stdout}{result.stderr}"),
        )
        status = {
            "last_run": started,
            "success": result.returncode == 0,
            "returncode": result.returncode,
        }
        duration_s = round(time.monotonic() - start_time, 2)
        log_fields = {**job_fields, **status, "duration_s": duration_s}
        if result.returncode == 0:
            logger.info("job finished", extra={"extra_fields": log_fields})
        else:
            # Surface the actual failure reason inline — the plaintext log file
            # has the full output, but "why" shouldn't need a second tool call.
            output, truncated = _tail(result.stderr or result.stdout)
            log_fields["output_tail"] = scrub_credentials(output)
            log_fields["output_truncated"] = truncated
            logger.error("job finished", extra={"extra_fields": log_fields})
    except subprocess.TimeoutExpired:
        status = {"last_run": started, "success": False, "returncode": None, "error": "timeout"}
        storage.append_log(name, f"\n=== run {started} TIMED OUT after {timeout}s ===\n")
        timeout_fields = {**job_fields, "timeout_s": timeout}
        if docker_host:
            # The killed local subprocess does not stop a remote container it
            # started (e.g. via `docker run`) — it's still running on the
            # Docker host DOCKER_HOST points at until it exits or is reaped.
            timeout_fields["note"] = "remote container (if any) may still be running on the Docker host"
        logger.error("job timed out", extra={"extra_fields": timeout_fields})
    except subprocess.CalledProcessError as e:
        stderr = scrub_credentials(e.stderr or "")
        status = {"last_run": started, "success": False, "returncode": e.returncode, "error": stderr}
        storage.append_log(name, f"\n=== run {started} git sync failed ===\n{stderr}\n")
        logger.error(
            "job git sync failed",
            extra={"extra_fields": {**job_fields, "returncode": e.returncode, "stderr": stderr}},
        )
    except Exception as e:
        status = {"last_run": started, "success": False, "returncode": None, "error": str(e)}
        storage.append_log(name, f"\n=== run {started} unexpected error ===\n{e}\n")
        logger.error("job failed unexpectedly", extra={"extra_fields": job_fields}, exc_info=True)

    storage.write_job_status(name, status)
    return status
