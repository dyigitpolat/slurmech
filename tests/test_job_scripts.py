"""Behavioral tests: rendered job scripts are executed with real bash locally.

#SBATCH directives are plain comments, so the scripts run unmodified. No SSH,
no Slurm: the scripts are exercised against a tmp_path layout.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from slurmech.config import EnvConfig, SlurmConfig
from slurmech.jobs import RemoteLayout, render_job_script, render_pack_script
from slurmech.pack import PackChild, PackSpec

SLURM = SlurmConfig(time="00:10:00")
NO_ENV = EnvConfig(mode="reuse")  # reuse without venv renders a no-op activation


def _prepare(tmp_path: Path, run_id: str) -> tuple[RemoteLayout, Path]:
    layout = RemoteLayout(str(tmp_path))
    base = Path(layout.base)
    base.mkdir(parents=True)
    (base / "sentinel.txt").write_text("base")
    run_dir = Path(layout.run_dir(run_id))
    run_dir.mkdir(parents=True)
    return layout, run_dir


def _render_single(
    tmp_path: Path,
    cmd: list[str],
    env: EnvConfig = NO_ENV,
    run_id: str = "20260101-000000-testrun1",
) -> tuple[Path, Path]:
    layout, run_dir = _prepare(tmp_path, run_id)
    script = render_job_script(run_id, cmd, layout, SLURM, env)
    script_path = run_dir / "job.slurm.sh"
    script_path.write_text(script)
    return script_path, run_dir


def _render_pack(
    tmp_path: Path,
    spec: PackSpec,
    run_id: str = "20260101-000000-packtest",
) -> tuple[Path, Path]:
    layout, run_dir = _prepare(tmp_path, run_id)
    script = render_pack_script(run_id, spec, layout, SLURM, NO_ENV)
    script_path = run_dir / "job.slurm.sh"
    script_path.write_text(script)
    return script_path, run_dir


def _wait_for(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def _assert_pid_gone(pid: int, timeout: float = 10.0) -> None:
    def gone() -> bool:
        try:
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            return True

    _wait_for(gone, timeout)


def _assert_pgid_gone(pgid: int, timeout: float = 10.0) -> None:
    def gone() -> bool:
        try:
            os.killpg(pgid, 0)
            return False
        except ProcessLookupError:
            return True

    _wait_for(gone, timeout)


# --- rendered-content contracts -------------------------------------------------


def test_single_script_runs_command_in_own_process_group_with_traps() -> None:
    script = render_job_script(
        "20260101-000000-abcd1234",
        ["python", "train.py"],
        RemoteLayout("/remote/work"),
        SLURM,
        NO_ENV,
    )
    assert "setsid bash -c" in script
    assert "CMD_PID=$!" in script
    assert 'kill -TERM -- "-$pgid"' in script
    assert 'kill -KILL -- "-$pgid"' in script
    assert "trap on_term TERM" in script
    assert "trap on_int INT" in script
    assert "trap on_exit EXIT" in script
    assert ".timeout" in script


def test_pack_script_tracks_child_process_groups() -> None:
    spec = PackSpec(jobs=[PackChild(name="a", cmd="echo a")], parallelism=1)
    script = render_pack_script(
        "20260101-000000-pack1234", spec, RemoteLayout("/remote/work"), SLURM, NO_ENV
    )
    assert "setsid bash -lc" in script
    assert '> "$child_dir/pgid"' in script
    assert 'kill -TERM -- "-$pgid"' in script
    assert 'kill -KILL -- "-$pgid"' in script
    assert "trap on_term TERM" in script
    assert "trap on_exit EXIT" in script


# --- single-job behavior --------------------------------------------------------


def test_single_job_success_writes_finished_markers(tmp_path: Path) -> None:
    script_path, run_dir = _render_single(tmp_path, ["true"])

    proc = subprocess.run(["bash", str(script_path)], timeout=60)

    assert proc.returncode == 0
    assert (run_dir / "exitcode").read_text().strip() == "0"
    assert (run_dir / ".finished").exists()
    assert not (run_dir / ".failed").exists()
    assert not (run_dir / ".timeout").exists()
    assert not (run_dir / ".running").exists()
    assert (run_dir / "workspace" / "sentinel.txt").exists()


def test_single_job_failure_writes_failed_marker_and_exitcode(tmp_path: Path) -> None:
    script_path, run_dir = _render_single(tmp_path, ["bash", "-c", "exit 7"])

    proc = subprocess.run(["bash", str(script_path)], timeout=60)

    assert proc.returncode == 7
    assert (run_dir / "exitcode").read_text().strip() == "7"
    assert (run_dir / ".failed").exists()
    assert not (run_dir / ".finished").exists()
    assert not (run_dir / ".running").exists()


def test_single_job_setup_failure_still_writes_failure_markers(tmp_path: Path) -> None:
    env = EnvConfig(mode="script", script="missing-env-setup.sh")
    script_path, run_dir = _render_single(tmp_path, ["true"], env=env)

    proc = subprocess.run(["bash", str(script_path)], timeout=60)

    assert proc.returncode != 0
    assert (run_dir / ".failed").exists()
    assert (run_dir / "exitcode").read_text().strip() != "0"
    assert not (run_dir / ".running").exists()


def test_single_job_reaps_lingering_descendants(tmp_path: Path) -> None:
    pid_file = tmp_path / "sleeper.pid"
    payload = f"sleep 300 & echo $! > {pid_file}; exit 0"
    script_path, run_dir = _render_single(tmp_path, ["bash", "-c", payload])

    start = time.monotonic()
    proc = subprocess.run(["bash", str(script_path)], timeout=60)
    elapsed = time.monotonic() - start

    assert proc.returncode == 0
    assert (run_dir / ".finished").exists()
    assert elapsed < 30, "lingering descendant must not hold the allocation"
    _assert_pid_gone(int(pid_file.read_text().strip()))


def test_single_job_sigterm_writes_timeout_marker_and_kills_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "payload.pid"
    payload = f"echo $$ > {pid_file}; sleep 300"
    script_path, run_dir = _render_single(tmp_path, ["bash", "-c", payload])

    proc = subprocess.Popen(["bash", str(script_path)])
    try:
        _wait_for(pid_file.exists)
        proc.send_signal(signal.SIGTERM)
        returncode = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert returncode == 143
    assert (run_dir / ".timeout").exists()
    assert (run_dir / "exitcode").read_text().strip() == "143"
    assert not (run_dir / ".running").exists()
    assert not (run_dir / ".finished").exists()
    _assert_pid_gone(int(pid_file.read_text().strip()))


# --- pack behavior ---------------------------------------------------------------


def test_pack_success_writes_child_exitcodes_and_finished(tmp_path: Path) -> None:
    spec = PackSpec(
        jobs=[PackChild(name="a", cmd="echo a"), PackChild(name="b", cmd="echo b")],
        parallelism=2,
    )
    script_path, run_dir = _render_pack(tmp_path, spec)

    proc = subprocess.run(["bash", str(script_path)], timeout=60)

    assert proc.returncode == 0
    assert (run_dir / "children" / "a" / "exitcode").read_text().strip() == "0"
    assert (run_dir / "children" / "b" / "exitcode").read_text().strip() == "0"
    assert (run_dir / ".finished").exists()
    assert not (run_dir / ".running").exists()
    status = (run_dir / "children" / "status.tsv").read_text()
    assert "a 0" in status
    assert "b 0" in status


def test_pack_child_environment_persists_after_cd(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    observed = tmp_path / "observed.txt"
    spec = PackSpec(
        jobs=[
            PackChild(
                name="env",
                cmd=f"cd {nested} && printf %s \"$SCIENTIFIC_TOKEN\" > {observed}",
                env={"SCIENTIFIC_TOKEN": "persisted"},
            )
        ],
        parallelism=1,
    )
    script_path, _run_dir = _render_pack(tmp_path, spec)

    result = subprocess.run(["bash", str(script_path)], check=False)

    assert result.returncode == 0
    assert observed.read_text() == "persisted"


def test_pack_kill_on_failure_kills_sibling_process_group(tmp_path: Path) -> None:
    flag = tmp_path / "victim.started"
    spec = PackSpec(
        jobs=[
            PackChild(name="boom", cmd="sleep 1; exit 3"),
            PackChild(name="victim", cmd=f"sleep 300 & echo started > {flag}; wait"),
        ],
        parallelism=2,
        fail_fast=True,
        kill_on_failure=True,
    )
    script_path, run_dir = _render_pack(tmp_path, spec)

    start = time.monotonic()
    proc = subprocess.run(["bash", str(script_path)], timeout=60)
    elapsed = time.monotonic() - start

    assert proc.returncode == 3
    assert (run_dir / "exitcode").read_text().strip() == "3"
    assert (run_dir / ".failed").exists()
    assert (run_dir / "children" / "boom" / "exitcode").read_text().strip() == "3"
    assert elapsed < 30, "kill_on_failure must terminate the victim's whole group"
    victim_pgid = int((run_dir / "children" / "victim" / "pgid").read_text().strip())
    _assert_pgid_gone(victim_pgid)


def test_pack_sigterm_writes_timeout_and_kills_child_groups(tmp_path: Path) -> None:
    spec = PackSpec(jobs=[PackChild(name="main", cmd="sleep 300")], parallelism=1)
    script_path, run_dir = _render_pack(tmp_path, spec)
    pgid_file = run_dir / "children" / "main" / "pgid"

    proc = subprocess.Popen(["bash", str(script_path)])
    try:
        _wait_for(pgid_file.exists)
        proc.send_signal(signal.SIGTERM)
        returncode = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert returncode == 143
    assert (run_dir / ".timeout").exists()
    assert (run_dir / "exitcode").read_text().strip() == "143"
    assert not (run_dir / ".running").exists()
    _assert_pgid_gone(int(pgid_file.read_text().strip()))
