"""CLI-level tests for status --json, fetch --path, and marker reconciliation."""

from __future__ import annotations

import io
import json
import shlex
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from fakes import FakeConn
from slurmech.cli import _finalize_streamed_run, app
from slurmech.config import WorkspaceConfig
from slurmech.jobs import RemoteLayout
from slurmech.registry import Registry, RunRecord

runner = CliRunner()

REMOTE_ROOT = "/remote/work/.slurmech"


@pytest.fixture()
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home_dir = tmp_path / "home" / ".slurmech"
    monkeypatch.setattr("slurmech.registry.slurmech_home", lambda: home_dir)
    return home_dir


def _install_fake_connect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, conn: FakeConn
) -> WorkspaceConfig:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    config = WorkspaceConfig(root=project, profile="demo")
    credentials = SimpleNamespace(user="yigit")
    layout = RemoteLayout("/remote/work")
    monkeypatch.setattr(
        "slurmech.cli._connect", lambda profile: (config, credentials, conn, layout)
    )
    return config


def _run_dir(run_id: str) -> str:
    return f"{REMOTE_ROOT}/runs/{run_id}"


# --- status --json ---------------------------------------------------------------


def _seed_status_registry() -> None:
    registry = Registry("demo")
    registry.add_run(
        RunRecord(
            run_id="run1",
            cmd=["python", "run.py"],
            profile="demo",
            job_id="101",
            state="RUNNING",
            remote_run_dir=_run_dir("run1"),
        )
    )
    registry.add_run(
        RunRecord(
            run_id="pack1",
            cmd=["pack", "jobs.yaml"],
            profile="demo",
            job_id="102",
            state="RUNNING",
            remote_run_dir=_run_dir("pack1"),
            meta={
                "kind": "pack",
                "children": [
                    {"name": "a", "exitcode": "children/a/exitcode"},
                    {"name": "b", "exitcode": "children/b/exitcode"},
                ],
            },
        )
    )


def test_status_json_emits_one_json_object_per_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
) -> None:
    conn = FakeConn(
        files={
            f"{_run_dir('run1')}/.failed": b"",
            f"{_run_dir('pack1')}/.finished": b"",
            f"{_run_dir('pack1')}/children/a/exitcode": b"0\n",
            f"{_run_dir('pack1')}/children/b/exitcode": b"1\n",
        }
    )
    _install_fake_connect(monkeypatch, tmp_path, conn)
    _seed_status_registry()

    result = runner.invoke(app, ["status", "--all", "--json"])

    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    payloads = [json.loads(line) for line in lines]
    assert len(payloads) == 2
    by_id = {item["run_id"]: item for item in payloads}
    assert by_id["run1"]["state"] == "FAILED"
    assert by_id["run1"]["job_id"] == "101"
    assert by_id["run1"]["cmd"] == ["python", "run.py"]
    assert by_id["run1"]["created_at"]
    assert "children" not in by_id["run1"]
    assert by_id["pack1"]["state"] == "FINISHED"
    assert by_id["pack1"]["children"] == {"total": 2, "completed": 2, "failed": 1}
    assert "Profile:" not in result.stdout


def test_status_json_marks_vanished_job_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
) -> None:
    conn = FakeConn(
        files={f"{_run_dir('run1')}/.running": b""},
        bash_handler=lambda command: (1, "", "") if command.startswith("squeue") else None,
    )
    _install_fake_connect(monkeypatch, tmp_path, conn)
    registry = Registry("demo")
    registry.add_run(
        RunRecord(
            run_id="run1",
            cmd=["python", "run.py"],
            profile="demo",
            job_id="101",
            state="RUNNING",
            remote_run_dir=_run_dir("run1"),
        )
    )

    result = runner.invoke(app, ["status", "--all", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout.strip())
    assert payload["state"] == "STALE"
    assert Registry("demo").find_run(run_id="run1")["state"] == "STALE"


def test_status_json_hides_terminal_runs_without_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
) -> None:
    conn = FakeConn(files={f"{_run_dir('run1')}/.timeout": b""})
    _install_fake_connect(monkeypatch, tmp_path, conn)
    registry = Registry("demo")
    registry.add_run(
        RunRecord(
            run_id="run1",
            cmd=["python", "run.py"],
            profile="demo",
            job_id="101",
            state="RUNNING",
            remote_run_dir=_run_dir("run1"),
        )
    )

    result = runner.invoke(app, ["status", "--json"])

    assert result.exit_code == 0
    assert result.stdout.strip() == ""
    assert Registry("demo").find_run(run_id="run1")["state"] == "TIMEOUT"


def test_status_plain_output_still_prints_profile_and_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
) -> None:
    conn = FakeConn(
        files={f"{_run_dir('run1')}/.running": b""},
        bash_handler=lambda command: (0, "RUNNING", "")
        if command.startswith("squeue")
        else None,
    )
    _install_fake_connect(monkeypatch, tmp_path, conn)
    registry = Registry("demo")
    registry.add_run(
        RunRecord(
            run_id="run1",
            cmd=["python", "run.py"],
            profile="demo",
            job_id="101",
            state="RUNNING",
            remote_run_dir=_run_dir("run1"),
        )
    )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Profile: demo" in result.stdout
    assert "run1" in result.stdout
    assert "state=RUNNING" in result.stdout


# --- fetch --path ----------------------------------------------------------------


def _tar_bytes_of(source: Path, arcnames: list[str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for arcname in arcnames:
            archive.add(source / arcname, arcname=arcname)
    return buffer.getvalue()


def _seed_fetch_run() -> None:
    registry = Registry("demo")
    registry.add_run(
        RunRecord(
            run_id="run1",
            cmd=["python", "run.py"],
            profile="demo",
            job_id="101",
            state="FINISHED",
            remote_run_dir=_run_dir("run1"),
        )
    )




def _noisy_sentinel_output(lines: list[str]) -> str:
    """Remote stdout as produced under a .bashrc that echoes noise."""
    return "\n".join(
        ["hello", "__SLURMECH_STDOUT_BEGIN__", *lines, "__SLURMECH_STDOUT_END__", ""]
    )


def test_fetch_path_extracts_matched_globs_into_workspace_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
) -> None:
    remote_ws = tmp_path / "remote_ws"
    state_dir = remote_ws / "generated" / "foo_phased_deployment_run" / "_GUI_STATE"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text('{"acc": 0.97}')
    workspace = f"{_run_dir('run1')}/workspace"
    pattern = "generated/*_phased_deployment_run/_GUI_STATE"

    conn = FakeConn()

    def bash_handler(command: str):
        # Every remote shell emits .bashrc noise ("hello") before real output.
        if "compgen -G" in command:
            if shlex.quote(pattern) in command:
                return (0, _noisy_sentinel_output(
                    ["generated/foo_phased_deployment_run/_GUI_STATE"]), "")
            return (1, _noisy_sentinel_output([]), "")
        if "tar czf" in command:
            assert f"cd {shlex.quote(workspace)} && tar czf /tmp/slurmech_fetch_" in command
            assert shlex.quote("generated/foo_phased_deployment_run/_GUI_STATE") in command
            remote_tmp = command.split("tar czf ", 1)[1].split(" -- ", 1)[0]
            conn.files[remote_tmp] = _tar_bytes_of(remote_ws, ["generated"])
            return (0, "hello\n", "")
        if command.startswith("rm -f "):
            return (0, "hello\n", "")
        return None

    conn.bash_handler = bash_handler
    _install_fake_connect(monkeypatch, tmp_path, conn)
    _seed_fetch_run()

    result = runner.invoke(app, ["fetch", "run1", "--path", pattern])

    assert result.exit_code == 0
    extracted = (
        home
        / "workspaces"
        / "demo"
        / "runs"
        / "run1"
        / "artifacts"
        / "workspace"
        / "generated"
        / "foo_phased_deployment_run"
        / "_GUI_STATE"
        / "state.json"
    )
    assert extracted.read_text() == '{"acc": 0.97}'


def test_fetch_path_exits_nonzero_when_no_glob_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
) -> None:
    def bash_handler(command: str):
        if "compgen -G" in command:
            return (1, "", "")
        return None

    conn = FakeConn(bash_handler=bash_handler)
    _install_fake_connect(monkeypatch, tmp_path, conn)
    _seed_fetch_run()

    result = runner.invoke(app, ["fetch", "run1", "--path", "generated/nothing/*"])

    assert result.exit_code != 0
    assert "No remote match" in result.stdout


def test_fetch_path_partial_match_warns_but_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
) -> None:
    remote_ws = tmp_path / "remote_ws"
    (remote_ws / "results").mkdir(parents=True)
    (remote_ws / "results" / "out.txt").write_text("ok")

    conn = FakeConn()

    def bash_handler(command: str):
        if "compgen -G" in command:
            if shlex.quote("results/*") in command:
                return (0, _noisy_sentinel_output(["results/out.txt"]), "")
            return (1, _noisy_sentinel_output([]), "")
        if "tar czf" in command:
            remote_tmp = command.split("tar czf ", 1)[1].split(" -- ", 1)[0]
            conn.files[remote_tmp] = _tar_bytes_of(remote_ws, ["results/out.txt"])
            return (0, "hello\n", "")
        if command.startswith("rm -f "):
            return (0, "hello\n", "")
        return None

    conn.bash_handler = bash_handler
    _install_fake_connect(monkeypatch, tmp_path, conn)
    _seed_fetch_run()

    result = runner.invoke(
        app, ["fetch", "run1", "--path", "results/*", "--path", "missing/*"]
    )

    assert result.exit_code == 0
    assert "No remote match" in result.stdout
    extracted = (
        home / "workspaces" / "demo" / "runs" / "run1" / "artifacts" / "workspace"
        / "results" / "out.txt"
    )
    assert extracted.read_text() == "ok"


def test_fetch_without_path_keeps_fixed_log_set_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
) -> None:
    conn = FakeConn(
        files={
            f"{_run_dir('run1')}/stdout.log": b"hello\n",
            f"{_run_dir('run1')}/exitcode": b"0\n",
            f"{_run_dir('run1')}/.finished": b"",
        }
    )
    _install_fake_connect(monkeypatch, tmp_path, conn)
    _seed_fetch_run()

    result = runner.invoke(app, ["fetch", "run1"])

    assert result.exit_code == 0
    artifacts = home / "workspaces" / "demo" / "runs" / "run1" / "artifacts"
    assert (artifacts / "stdout.log").read_bytes() == b"hello\n"
    assert (artifacts / "exitcode").read_bytes() == b"0\n"


# --- streaming end-state reconciliation ------------------------------------------


def _seed_single_run(state: str = "SUBMITTED") -> None:
    registry = Registry("demo")
    registry.add_run(
        RunRecord(
            run_id="run1",
            cmd=["python", "run.py"],
            profile="demo",
            job_id="101",
            state=state,
            remote_run_dir=_run_dir("run1"),
        )
    )


@pytest.mark.parametrize(
    ("marker", "expected"),
    [(".finished", "FINISHED"), (".failed", "FAILED"), (".timeout", "TIMEOUT")],
)
def test_finalize_streamed_run_reads_terminal_markers(
    home: Path, marker: str, expected: str
) -> None:
    conn = FakeConn(files={f"{_run_dir('run1')}/{marker}": b""})
    _seed_single_run()
    registry = Registry("demo")

    state = _finalize_streamed_run(conn, registry, "run1", _run_dir("run1"))

    assert state == expected
    assert Registry("demo").find_run(run_id="run1")["state"] == expected


def test_finalize_streamed_run_marks_stale_when_markers_missing(home: Path) -> None:
    conn = FakeConn()
    _seed_single_run()
    registry = Registry("demo")

    state = _finalize_streamed_run(conn, registry, "run1", _run_dir("run1"))

    assert state == "STALE"
    assert Registry("demo").find_run(run_id="run1")["state"] == "STALE"


def test_finalize_streamed_run_marks_stale_on_leftover_running_marker(home: Path) -> None:
    conn = FakeConn(files={f"{_run_dir('run1')}/.running": b""})
    _seed_single_run()
    registry = Registry("demo")

    state = _finalize_streamed_run(conn, registry, "run1", _run_dir("run1"))

    assert state == "STALE"


def test_run_command_reconciles_state_from_markers_after_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
) -> None:
    conn = FakeConn(
        files={f"{_run_dir('runX')}/.timeout": b""},
        dirs={f"{REMOTE_ROOT}/base"},
    )
    _install_fake_connect(monkeypatch, tmp_path, conn)
    monkeypatch.setattr("slurmech.cli.new_run_id", lambda: "runX")
    monkeypatch.setattr(
        "slurmech.cli.submit_job", lambda *args, **kwargs: ("555", "Submitted batch job 555")
    )
    monkeypatch.setattr(
        "slurmech.cli.stream_stdout_until_done", lambda *args, **kwargs: None
    )

    result = runner.invoke(app, ["run", "--", "python", "run.py"])

    assert result.exit_code == 0
    run = Registry("demo").find_run(run_id="runX")
    assert run is not None
    assert run["state"] == "TIMEOUT"


def test_pack_command_reconciles_state_from_markers_after_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
) -> None:
    conn = FakeConn(
        files={f"{_run_dir('runP')}/.failed": b""},
        dirs={f"{REMOTE_ROOT}/base"},
    )
    _install_fake_connect(monkeypatch, tmp_path, conn)
    monkeypatch.setattr("slurmech.cli.new_run_id", lambda: "runP")
    monkeypatch.setattr(
        "slurmech.cli.submit_pack_job",
        lambda *args, **kwargs: ("556", "Submitted batch job 556"),
    )
    monkeypatch.setattr(
        "slurmech.cli.stream_stdout_until_done", lambda *args, **kwargs: None
    )
    pack_file = tmp_path / "jobs.yaml"
    pack_file.write_text(
        """
jobs:
  - name: a
    cmd: echo a
"""
    )

    result = runner.invoke(app, ["pack", str(pack_file)])

    assert result.exit_code == 0
    run = Registry("demo").find_run(run_id="runP")
    assert run is not None
    assert run["state"] == "FAILED"
    assert run["meta"]["kind"] == "pack"
