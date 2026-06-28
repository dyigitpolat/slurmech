"""Remote Slurm and path helpers."""

from __future__ import annotations

import posixpath
import time

from slurmech.ssh import SSHConnection


def resolve_remote_path(conn: SSHConnection, path: str) -> str:
    if path.startswith("/"):
        return path

    rc, out, _ = conn.bash("echo $HOME")
    if rc != 0:
        return path
    candidates = [line.strip() for line in out.splitlines() if line.strip().startswith("/")]
    if not candidates:
        return path
    home = candidates[-1]

    if path == "~":
        return home
    if path.startswith("~"):
        return posixpath.join(home, path.lstrip("~/"))
    return posixpath.join(home, path)


def parse_job_id(sbatch_output: str) -> str:
    for token in sbatch_output.strip().split():
        if token.isdigit():
            return token
    raise ValueError(f"Could not parse job id from sbatch output: {sbatch_output!r}")


def remote_exists(conn: SSHConnection, path: str) -> bool:
    try:
        return conn.exists(path)
    except Exception:
        return False


def run_state_from_markers(conn: SSHConnection, run_dir: str) -> str:
    if remote_exists(conn, posixpath.join(run_dir, ".cancelled")):
        return "CANCELLED"
    if remote_exists(conn, posixpath.join(run_dir, ".finished")):
        return "FINISHED"
    if remote_exists(conn, posixpath.join(run_dir, ".running")):
        return "RUNNING"
    if remote_exists(conn, posixpath.join(run_dir, ".pending")):
        return "PENDING"
    return "UNKNOWN"


def squeue_state(conn: SSHConnection, job_id: str) -> str | None:
    rc, out, _ = conn.bash(f"squeue -h -j {job_id} -o %T")
    if rc != 0:
        return None
    valid_states = {
        "PENDING",
        "RUNNING",
        "COMPLETED",
        "COMPLETING",
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
        "SUSPENDED",
        "CONFIGURING",
        "RESIZING",
    }
    for line in out.splitlines():
        token = line.strip().split(maxsplit=1)[0].upper() if line.strip() else ""
        if token in valid_states:
            return token
    return None


def wait_for_job(conn: SSHConnection, job_id: str, poll_seconds: int = 10) -> None:
    while True:
        rc, out, _ = conn.bash(f"squeue -h -j {job_id}")
        if rc != 0 or str(job_id) not in out:
            return
        time.sleep(poll_seconds)
