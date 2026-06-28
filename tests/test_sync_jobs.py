from pathlib import Path

from slurmech.config import EnvConfig, SlurmConfig, SyncConfig, WorkspaceConfig
from slurmech.jobs import RemoteLayout, render_job_script, render_pack_script
from slurmech.pack import PackChild, PackSpec
from slurmech.sync import build_manifest, changed_files, select_files


def test_select_files_uses_git_tracked_files_and_globs(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')")
    (tmp_path / "data.bin").write_text("large")

    # Force fallback path by making this not a real git repo; include/exclude still applies.
    config = WorkspaceConfig(
        root=tmp_path,
        profile="demo",
        sync=SyncConfig(include=["src/**", "data.bin"], exclude=["data.bin"]),
    )

    assert select_files(config) == [Path("src/main.py")]


def test_manifest_diff_detects_changed_files(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("one")
    first = build_manifest(tmp_path, [Path("a.txt")])

    file_path.write_text("two")
    second = build_manifest(tmp_path, [Path("a.txt")])

    assert changed_files(second, first) == [Path("a.txt")]


def test_render_job_script_uses_hardlink_overlay_and_stdio_logs() -> None:
    script = render_job_script(
        run_id="20260101-000000-abcd1234",
        cmd=["python", "train.py", "--epochs", "1"],
        layout=RemoteLayout("/remote/work"),
        slurm=SlurmConfig(partition="gpu", time="00:10:00", gres="gpu:1", mem="16G"),
        env=EnvConfig(mode="reuse", venv="/remote/env"),
    )

    assert "#SBATCH --partition=gpu" in script
    assert "cp -al \"$BASE\"/. \"$WORKSPACE\"/" in script
    assert "rsync -a \"$OVERLAY\"/ \"$WORKSPACE\"/" in script
    assert "source /remote/env/bin/activate" in script
    assert "python train.py --epochs 1" in script
    assert "> \"$RUN_DIR/stdout.log\" 2> \"$RUN_DIR/stderr.log\"" in script


def test_render_pack_script_runs_children_concurrently_with_logs() -> None:
    spec = PackSpec(
        jobs=[
            PackChild(name="a", cmd="python -c 'print(1)'"),
            PackChild(name="b", cmd="python -c 'print(2)'", env={"CUDA_VISIBLE_DEVICES": "0"}),
        ],
        parallelism=2,
        fail_fast=True,
        kill_on_failure=True,
    )

    script = render_pack_script(
        run_id="20260101-000000-pack1234",
        spec=spec,
        layout=RemoteLayout("/remote/work"),
        slurm=SlurmConfig(partition="gpu", time="00:10:00", gres="gpu:1", mem="16G"),
        env=EnvConfig(mode="reuse", venv="/remote/env"),
    )

    assert "#SBATCH --job-name=slurmech_pack_pack1234" in script
    assert "PARALLELISM=2" in script
    assert "FAIL_FAST=1" in script
    assert "KILL_ON_FAILURE=1" in script
    assert "local child_dir=\"$RUN_DIR/children/$name\"" in script
    assert "> \"$child_dir/stdout.log\" 2> \"$child_dir/stderr.log\"" in script
    assert "echo \"$exit_code\" > \"$child_dir/exitcode\"" in script
    assert "run_child \"$name\" \"$cmd\" &" in script
    assert "CUDA_VISIBLE_DEVICES=0 python -c" in script
    assert "cp -al \"$BASE\"/. \"$WORKSPACE\"/" in script
