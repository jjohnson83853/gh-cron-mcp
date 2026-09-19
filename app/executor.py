import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import storage
from .logging_config import scrub_credentials

logger = logging.getLogger("gh_cron_mcp")

_TAIL_CHARS = 2000

# APScheduler's max_instances=1 only protects a job against overlapping with
# itself *when triggered by the scheduler*. It has no visibility into
# run_job_now, which calls this function directly — so a manual trigger can
# still race a scheduled fire (or another manual trigger) of the same job.
# This guard lives here, inside the plain module-level function, rather than
# on SchedulerService, because SQLAlchemyJobStore persists the scheduled
# callable by reference for restart-recovery — routing scheduled execution
# through a bound method on a stateful object (holding live locks/threads)
# would break that pickling.
_running_jobs: set[str] = set()
_running_lock = threading.Lock()


class JobAlreadyRunning(Exception):
    pass


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


_DOCKER_CMD_TIMEOUT_S = 15
_DOCKER_STOP_GRACE_S = 10


def _docker_ps_ids(env: dict) -> Optional[set[str]]:
    """IDs of *running* containers on the docker host `env` points at.

    Returns None when the docker CLI is missing or the query fails — callers
    must treat None as "cleanup impossible", never as "no containers".
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "-q"], env=env, capture_output=True, text=True,
            timeout=_DOCKER_CMD_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning(
            "docker ps failed, orphan cleanup unavailable",
            extra={"extra_fields": {"error": type(e).__name__}},
        )
        return None
    if result.returncode != 0:
        logger.warning(
            "docker ps failed, orphan cleanup unavailable",
            extra={"extra_fields": {"stderr": scrub_credentials((result.stderr or "")[-_TAIL_CHARS:])}},
        )
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _stop_and_remove(container_id: str, env: dict) -> bool:
    """Best-effort stop+rm of one orphaned container; False if any step failed."""
    ok = True
    for cmd in (
        ["docker", "stop", "--time", str(_DOCKER_STOP_GRACE_S), container_id],
        ["docker", "rm", container_id],
    ):
        try:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=_DOCKER_CMD_TIMEOUT_S)
            if result.returncode != 0:
                ok = False
                logger.warning(
                    "docker cleanup step failed",
                    extra={"extra_fields": {"container_id": container_id, "cmd": cmd[0] + " " + cmd[1],
                                            "stderr": scrub_credentials((result.stderr or "")[-_TAIL_CHARS:])}},
                )
        except (OSError, subprocess.TimeoutExpired) as e:
            ok = False
            logger.warning(
                "docker cleanup step failed",
                extra={"extra_fields": {"container_id": container_id, "cmd": cmd[0] + " " + cmd[1],
                                        "error": str(e)}},
            )
    return ok


def _reap_orphaned_containers(pre_ids: set[str], env: dict) -> Optional[list[str]]:
    """Stop+rm every running container that appeared since `pre_ids`.

    Returns the reaped ids, or None when the host became unreachable
    (cleanup impossible — caller must not treat that as "nothing to do").
    """
    current = _docker_ps_ids(env)
    if current is None:
        return None
    orphans = sorted(current - pre_ids)
    return [cid for cid in orphans if _stop_and_remove(cid, env)]


def run_job(
    name: str,
    repo_url: str,
    ref: str,
    entrypoint: str,
    env_vars: Optional[dict],
    token: Optional[str],
    timeout: int = 1800,
) -> dict:
    with _running_lock:
        if name in _running_jobs:
            logger.warning(
                "job trigger skipped, already running",
                extra={"extra_fields": {"job": name}},
            )
            raise JobAlreadyRunning(name)
        _running_jobs.add(name)

    storage.ensure_dirs()
    dest = storage.repo_path(name)
    started = datetime.now(timezone.utc).isoformat()
    env = {**os.environ, **(env_vars or {})}
    job_fields = {"job": name, "repo_url": repo_url, "ref": ref, "entrypoint": entrypoint, "run_id": started}
    docker_host = (env_vars or {}).get("DOCKER_HOST")
    if docker_host:
        job_fields["docker_host"] = docker_host
    start_time = time.monotonic()
    pre_container_ids: Optional[set[str]] = None

    logger.info("job started", extra={"extra_fields": job_fields})

    try:
        try:
            sync_repo(repo_url, ref, dest, token)
            job_fields["commit"] = _head_commit(dest)
            # Delta-diff against a pre-run snapshot so cleanup touches only
            # containers THIS run started — but on this shared host a
            # container another process starts mid-run is an accepted
            # false positive (no attribution exists).
            pre_container_ids = _docker_ps_ids(env) if env.get("DOCKER_HOST") else None
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
            # A killed local subprocess does not stop a remote container it
            # started — reap what the run started so the host is not left
            # with orphans.
            if pre_container_ids is not None:
                orphans = _reap_orphaned_containers(pre_container_ids, env)
                if orphans is not None:
                    timeout_fields["orphaned_containers_reaped"] = orphans
                else:
                    timeout_fields["note"] = "docker unreachable during orphan cleanup"
            elif env.get("DOCKER_HOST"):
                timeout_fields["note"] = "no pre-run container snapshot; remote container (if any) may still be running"
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
    finally:
        with _running_lock:
            _running_jobs.discard(name)
