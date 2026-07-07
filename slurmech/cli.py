"""CLI entry point for slurmech."""

from __future__ import annotations

import shlex
import sys
from dataclasses import replace
from pathlib import Path

import typer

from slurmech.config import SlurmConfig, load_workspace_config
from slurmech.credentials import Credentials, load_credentials
from slurmech.jobs import RemoteLayout, new_run_id, submit_job, submit_pack_job
from slurmech.pack import PackSpec, load_pack_file
from slurmech.registry import Registry, RunRecord
from slurmech.remote import resolve_remote_path, run_state_from_markers, squeue_state
from slurmech.ssh import SSHConnection
from slurmech.stream import stream_stdout_until_done
from slurmech.sync import (
    build_manifest,
    changed_files,
    manifest_to_json,
    read_remote_manifest,
    select_files,
    upload_files,
    write_remote_manifest,
)

app = typer.Typer(
    name="slurmech",
    help="Run commands on Slurm clusters with workspace sync and live stdio.",
    no_args_is_help=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)


def _connect(profile: str | None) -> tuple:
    config = load_workspace_config(profile=profile)
    credentials = load_credentials(config)
    conn = SSHConnection(
        host=credentials.host,
        user=credentials.user,
        port=credentials.port,
        password=credentials.password,
        key_filename=credentials.key_filename,
        connect_host=credentials.connection_host,
        connect_port=credentials.connection_port,
        proxy_command=credentials.proxy_command,
    ).connect()
    remote_dir = resolve_remote_path(conn, credentials.remote_dir)
    layout = RemoteLayout(remote_dir)
    return config, credentials, conn, layout


def _slurm_with_overrides(
    base: SlurmConfig,
    partition: str | None = None,
    gres: str | None = None,
    time: str | None = None,
) -> SlurmConfig:
    return replace(
        base,
        partition=partition or base.partition,
        gres=gres or base.gres,
        time=time or base.time,
    )


def _submit_remote_command(
    profile: str | None,
    partition: str | None,
    gres: str | None,
    time: str | None,
    port: list[int] | None,
    detach: bool,
    cmd: list[str],
) -> None:
    config, _, conn, layout = _connect(profile)
    try:
        if port:
            typer.echo("--port is accepted but port forwarding is not implemented in the MVP yet.")
        if not conn.exists(layout.base):
            typer.echo(f"Workspace is not initialized on remote: {layout.base}")
            typer.echo("Run: slurmech init")
            raise typer.Exit(code=1)

        files = select_files(config)
        manifest = build_manifest(config.root, files)
        previous = read_remote_manifest(conn, layout.manifest)
        diff = changed_files(manifest, previous)
        run_id = new_run_id()
        upload_files(conn, config.root, diff, layout.overlay_dir(run_id))

        slurm = _slurm_with_overrides(config.slurm, partition=partition, gres=gres, time=time)
        job_id, _ = submit_job(conn, run_id, cmd, layout, slurm, config.env)

        registry = Registry(config.profile)
        registry.add_run(
            RunRecord(
                run_id=run_id,
                cmd=cmd,
                profile=config.profile,
                job_id=job_id,
                state="SUBMITTED",
                remote_run_dir=layout.run_dir(run_id),
            )
        )
        typer.echo(f"Submitted run {run_id} as job {job_id}")
        if not detach:
            stream_stdout_until_done(conn, f"{layout.run_dir(run_id)}/stdout.log", job_id, typer.echo)
            registry.update_run(run_id=run_id, state="FINISHED")
    finally:
        conn.close()


def _sync_run_overlay(conn, config, layout) -> tuple[str, int]:
    files = select_files(config)
    manifest = build_manifest(config.root, files)
    previous = read_remote_manifest(conn, layout.manifest)
    diff = changed_files(manifest, previous)
    run_id = new_run_id()
    upload_files(conn, config.root, diff, layout.overlay_dir(run_id))
    return run_id, len(diff)


def _submit_pack(
    pack_file: Path,
    profile: str | None,
    partition: str | None,
    gres: str | None,
    time: str | None,
    parallelism: int | None,
    fail_fast: bool | None,
    kill_on_failure: bool | None,
    detach: bool,
) -> None:
    config, _, conn, layout = _connect(profile)
    try:
        if not conn.exists(layout.base):
            typer.echo(f"Workspace is not initialized on remote: {layout.base}")
            typer.echo("Run: slurmech init")
            raise typer.Exit(code=1)

        spec = load_pack_file(pack_file, defaults=config.pack)
        if parallelism is not None or fail_fast is not None or kill_on_failure is not None:
            if parallelism is not None and parallelism < 1:
                raise typer.BadParameter("--parallelism must be >= 1")
            spec = replace(
                spec,
                parallelism=min(parallelism or spec.parallelism, len(spec.jobs)),
                fail_fast=spec.fail_fast if fail_fast is None else fail_fast,
                kill_on_failure=spec.kill_on_failure
                if kill_on_failure is None
                else kill_on_failure,
            )

        run_id, changed_count = _sync_run_overlay(conn, config, layout)
        slurm = _slurm_with_overrides(config.slurm, partition=partition, gres=gres, time=time)
        job_id, _ = submit_pack_job(conn, run_id, spec, layout, slurm, config.env)

        registry = Registry(config.profile)
        registry.add_run(
            RunRecord(
                run_id=run_id,
                cmd=["pack", str(pack_file)],
                profile=config.profile,
                job_id=job_id,
                state="SUBMITTED",
                remote_run_dir=layout.run_dir(run_id),
                meta={
                    "kind": "pack",
                    "pack_file": str(pack_file),
                    "parallelism": spec.parallelism,
                    "fail_fast": spec.fail_fast,
                    "kill_on_failure": spec.kill_on_failure,
                    "children": spec.child_meta,
                    "changed_files": changed_count,
                },
            )
        )
        typer.echo(
            f"Submitted pack run {run_id} as job {job_id} "
            f"({len(spec.jobs)} children, parallelism={spec.parallelism})"
        )
        if not detach:
            stream_stdout_until_done(conn, f"{layout.run_dir(run_id)}/stdout.log", job_id, typer.echo)
            registry.update_run(run_id=run_id, state="FINISHED")
    finally:
        conn.close()


def _pack_child_summary(conn, run: dict) -> str:
    meta = run.get("meta", {})
    if meta.get("kind") != "pack":
        return ""
    children = meta.get("children", [])
    remote_run_dir = run.get("remote_run_dir")
    completed = 0
    failed = 0
    for child in children:
        exitcode_path = f"{remote_run_dir}/{child['exitcode']}"
        if conn.exists(exitcode_path):
            completed += 1
            with conn.sftp().open(exitcode_path, "r") as file:
                code = file.read().decode("utf-8", "ignore").strip()
            if code and code != "0":
                failed += 1
    return f" children={completed}/{len(children)} failed={failed}"


@app.command()
def init(
    profile: str = typer.Option(None, "--profile", "-p", help="Workspace profile name"),
    force: bool = typer.Option(False, "--force", help="Re-run remote environment setup"),
) -> None:
    """Initialize remote workspace (first-time setup)."""
    config, _, conn, layout = _connect(profile)
    try:
        if conn.exists(layout.base) and not force:
            typer.echo(f"Workspace already initialized: {layout.base}")
            return

        conn.mkdirs(layout.base)
        conn.mkdirs(layout.runs)
        files = select_files(config)
        manifest = build_manifest(config.root, files)
        upload_files(conn, config.root, files, layout.base)
        write_remote_manifest(conn, layout.manifest, manifest)
        (config.profile_dir / "workspace.toml").parent.mkdir(parents=True, exist_ok=True)
        (config.profile_dir / "manifest.json").write_text(manifest_to_json(manifest))

        if config.env.mode == "uv":
            rc, out, err = conn.bash(f"cd {shlex.quote(layout.base)} && uv sync")
            if rc != 0:
                raise typer.BadParameter(err or out)
        elif config.env.mode == "script" and config.env.script:
            rc, out, err = conn.bash(
                f"cd {shlex.quote(layout.base)} && bash {shlex.quote(config.env.script)}"
            )
            if rc != 0:
                raise typer.BadParameter(err or out)

        typer.echo(f"Initialized {config.profile}: uploaded {len(files)} files to {layout.base}")
    finally:
        conn.close()


@app.command()
def sync(
    profile: str = typer.Option(None, "--profile", "-p"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show diff without uploading"),
) -> None:
    """Sync tracked workspace files to the cluster."""
    config, _, conn, layout = _connect(profile)
    try:
        files = select_files(config)
        manifest = build_manifest(config.root, files)
        previous = read_remote_manifest(conn, layout.manifest)
        changed = changed_files(manifest, previous)
        if dry_run:
            for path in changed:
                typer.echo(path.as_posix())
            return
        upload_files(conn, config.root, changed, layout.base)
        write_remote_manifest(conn, layout.manifest, manifest)
        config.profile_dir.mkdir(parents=True, exist_ok=True)
        (config.profile_dir / "manifest.json").write_text(manifest_to_json(manifest))
        typer.echo(f"Synced {len(changed)} changed files to {layout.base}")
    finally:
        conn.close()


@app.command()
def status(
    profile: str = typer.Option(None, "--profile", "-p"),
    all_runs: bool = typer.Option(False, "--all", help="Include fetched/completed runs"),
) -> None:
    """Show run registry and Slurm queue."""
    config, credentials, conn, _ = _connect(profile)
    try:
        registry = Registry(config.profile)
        typer.echo(f"Profile: {config.profile}")
        for run in registry.all_runs():
            remote_run_dir = run.get("remote_run_dir")
            state = run.get("state")
            if remote_run_dir and conn.exists(remote_run_dir):
                marker_state = run_state_from_markers(conn, remote_run_dir)
                if marker_state != "UNKNOWN":
                    state = marker_state
                    registry.update_run(run_id=run.get("run_id"), state=state)
            job_id = run.get("job_id")
            if state in {"PENDING", "RUNNING", "SUBMITTED"} and job_id:
                live_state = squeue_state(conn, str(job_id))
                if live_state is None:
                    state = "STALE"
                    registry.update_run(run_id=run.get("run_id"), state=state)
            if not all_runs and state in {"FINISHED", "FAILED", "CANCELLED", "STALE"}:
                continue
            child_summary = _pack_child_summary(conn, run)
            typer.echo(
                f"{run.get('run_id')} job={run.get('job_id')} "
                f"state={state}{child_summary} cmd={' '.join(run.get('cmd', []))}"
            )
        rc, out, err = conn.bash(f"squeue -u {shlex.quote(credentials.user)}")
        typer.echo(out if rc == 0 else err)
    finally:
        conn.close()


@app.command()
def attach(
    run_id: str,
    profile: str = typer.Option(None, "--profile", "-p"),
    child: str | None = typer.Option(None, "--child", help="Attach to a pack child stdout"),
) -> None:
    """Attach to a run's stdout stream."""
    config, _, conn, layout = _connect(profile)
    try:
        registry = Registry(config.profile)
        run = registry.find_run(run_id=run_id) or registry.find_run(job_id=run_id)
        if not run:
            raise typer.BadParameter(f"Unknown run/job id: {run_id}")
        remote_run_dir = run.get("remote_run_dir") or layout.run_dir(run["run_id"])
        job_id = run.get("job_id")
        if not job_id:
            raise typer.BadParameter(f"Run has no Slurm job id: {run_id}")
        stdout_path = f"{remote_run_dir}/stdout.log"
        if child:
            meta_children = run.get("meta", {}).get("children", [])
            child_record = next((item for item in meta_children if item.get("name") == child), None)
            if not child_record:
                raise typer.BadParameter(f"Unknown child for run {run_id}: {child}")
            stdout_path = f"{remote_run_dir}/{child_record['stdout']}"
        stream_stdout_until_done(conn, stdout_path, job_id, typer.echo)
    finally:
        conn.close()


@app.command()
def cancel(run_id: str, profile: str = typer.Option(None, "--profile", "-p")) -> None:
    """Cancel a run by run id or Slurm job id."""
    config, _, conn, layout = _connect(profile)
    try:
        registry = Registry(config.profile)
        run = registry.find_run(run_id=run_id) or registry.find_run(job_id=run_id)
        job_id = run.get("job_id") if run else run_id
        rc, out, err = conn.bash(f"scancel {shlex.quote(str(job_id))}")
        if rc != 0:
            raise typer.BadParameter(err or out)
        if run:
            remote_run_dir = run.get("remote_run_dir") or layout.run_dir(run["run_id"])
            conn.bash(f"touch {shlex.quote(remote_run_dir)}/.cancelled")
            registry.update_run(run_id=run["run_id"], state="CANCELLED")
        typer.echo(f"Cancelled {job_id}")
    finally:
        conn.close()


@app.command()
def fetch(run_id: str, profile: str = typer.Option(None, "--profile", "-p")) -> None:
    """Fetch basic run artifacts."""
    config, _, conn, layout = _connect(profile)
    try:
        registry = Registry(config.profile)
        run = registry.find_run(run_id=run_id) or registry.find_run(job_id=run_id)
        if not run:
            raise typer.BadParameter(f"Unknown run/job id: {run_id}")
        remote_run_dir = run.get("remote_run_dir") or layout.run_dir(run["run_id"])
        local_dir = registry.runs_dir / run["run_id"] / "artifacts"
        for name in ["stdout.log", "stderr.log", "exitcode", "job.slurm.sh"]:
            remote_path = f"{remote_run_dir}/{name}"
            if conn.exists(remote_path):
                conn.get_file(remote_path, local_dir / name)
        for child in run.get("meta", {}).get("children", []):
            child_dir = local_dir / "children" / child["name"]
            for key in ["stdout", "stderr", "exitcode"]:
                remote_path = f"{remote_run_dir}/{child[key]}"
                if conn.exists(remote_path):
                    conn.get_file(remote_path, child_dir / Path(child[key]).name)
        state = run_state_from_markers(conn, remote_run_dir)
        registry.update_run(
            run_id=run["run_id"],
            fetched=True,
            state=state if state != "UNKNOWN" else run.get("state"),
        )
        typer.echo(f"Fetched artifacts to {local_dir}")
    finally:
        conn.close()


@app.command()
def doctor(profile: str = typer.Option(None, "--profile", "-p")) -> None:
    """Check SSH, Slurm, remote workspace, and environment configuration."""
    config, credentials, conn, layout = _connect(profile)
    try:
        typer.echo(f"profile: {config.profile}")
        typer.echo(f"target: {credentials.display_target}")
        typer.echo(f"route: {credentials.display_route}")
        typer.echo(f"remote_dir: {layout.remote_dir}")
        rc, out, err = conn.bash("command -v sbatch && command -v squeue")
        typer.echo("slurm: ok" if rc == 0 else f"slurm: missing ({err or out})")
        typer.echo(f"base: {'ok' if conn.exists(layout.base) else 'missing'} ({layout.base})")
        if config.env.mode == "reuse" and config.env.venv:
            activate = f"{config.env.venv.rstrip('/')}/bin/activate"
            typer.echo(f"env: {'ok' if conn.exists(activate) else 'missing'} ({activate})")
        else:
            typer.echo(f"env: {config.env.mode}")
    finally:
        conn.close()


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def grid(
    grid_file: Path = typer.Argument(..., exists=True, readable=True),
    profile: str = typer.Option(None, "--profile", "-p", help="Workspace profile"),
    partition: str = typer.Option(None, "--partition", help="Slurm partition override"),
    gres: str = typer.Option(None, "--gres", help="Slurm GRES override, e.g. gpu:h100-47:1"),
    time: str = typer.Option(None, "--time", help="Slurm time limit, e.g. 02:00:00"),
) -> None:
    """Submit a YAML file containing `commands: [[...], ...]`."""
    import yaml

    data = yaml.safe_load(grid_file.read_text()) or {}
    commands = data.get("commands")
    if not isinstance(commands, list) or not commands:
        raise typer.BadParameter("Grid file must contain a non-empty `commands` list.")
    for command in commands:
        if isinstance(command, str):
            cmd = shlex.split(command)
        elif isinstance(command, list):
            cmd = [str(part) for part in command]
        else:
            raise typer.BadParameter("Each command must be a string or list of strings.")
        _submit_remote_command(profile, partition, gres, time, port=None, detach=True, cmd=cmd)


@app.command()
def pack(
    pack_file: Path = typer.Argument(..., exists=True, readable=True),
    profile: str = typer.Option(None, "--profile", "-p", help="Workspace profile"),
    partition: str = typer.Option(None, "--partition", help="Slurm partition override"),
    gres: str = typer.Option(None, "--gres", help="Slurm GRES override, e.g. gpu:h100-47:1"),
    time: str = typer.Option(None, "--time", help="Slurm time limit, e.g. 02:00:00"),
    parallelism: int | None = typer.Option(None, "--parallelism", help="Max child processes"),
    fail_fast: bool | None = typer.Option(None, "--fail-fast", help="Stop launching after first failure"),
    kill_on_failure: bool | None = typer.Option(None, "--kill-on-failure", help="Kill active children on failure"),
    detach: bool = typer.Option(False, "--detach", "-d", help="Submit without streaming parent log"),
) -> None:
    """Submit multiple child commands inside one Slurm allocation."""
    _submit_pack(
        pack_file=pack_file,
        profile=profile,
        partition=partition,
        gres=gres,
        time=time,
        parallelism=parallelism,
        fail_fast=fail_fast,
        kill_on_failure=kill_on_failure,
        detach=detach,
    )


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    ctx: typer.Context,
    profile: str = typer.Option(None, "--profile", "-p", help="Workspace profile"),
    partition: str = typer.Option(None, "--partition", help="Slurm partition override"),
    gres: str = typer.Option(None, "--gres", help="Slurm GRES override, e.g. gpu:h100-47:1"),
    time: str = typer.Option(None, "--time", help="Slurm time limit, e.g. 02:00:00"),
    port: list[int] = typer.Option(None, "--port", "-L", help="Forward remote port to localhost"),
    detach: bool = typer.Option(False, "--detach", "-d", help="Submit without streaming stdio"),
) -> None:
    """Run an arbitrary command remotely. Use `slurmech run -- <cmd>`."""
    cmd = list(ctx.args)
    if not cmd:
        raise typer.BadParameter("Missing command. Use: slurmech run -- <cmd>")
    _submit_remote_command(profile, partition, gres, time, port, detach, cmd)


@app.callback(invoke_without_command=True)
def run_command(
    ctx: typer.Context,
    profile: str = typer.Option(None, "--profile", "-p", help="Workspace profile"),
    partition: str = typer.Option(None, "--partition", help="Slurm partition override"),
    gres: str = typer.Option(None, "--gres", help="Slurm GRES override, e.g. gpu:h100-47:1"),
    time: str = typer.Option(None, "--time", help="Slurm time limit, e.g. 02:00:00"),
    port: list[int] = typer.Option(None, "--port", "-L", help="Forward remote port to localhost"),
    detach: bool = typer.Option(False, "--detach", "-d", help="Submit without streaming stdio"),
) -> None:
    """Run CMD on the Slurm cluster (default when arguments are given)."""
    if ctx.invoked_subcommand is not None:
        return
    cmd = list(ctx.args)
    if not cmd:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)
    _submit_remote_command(profile, partition, gres, time, port, detach, cmd)


if __name__ == "__main__":
    app()


def _rewrite_bare_command(argv: list[str]) -> list[str]:
    known_commands = {
        "init",
        "sync",
        "status",
        "attach",
        "cancel",
        "fetch",
        "doctor",
        "grid",
        "pack",
        "run",
    }
    if not argv or argv[0] in {"--help", "-h"}:
        return argv

    options_with_values = {
        "--profile",
        "-p",
        "--partition",
        "--gres",
        "--time",
        "--port",
        "-L",
    }
    idx = 0
    while idx < len(argv):
        token = argv[idx]
        if token == "--":
            idx += 1
            break
        if token in known_commands:
            return argv
        if token in options_with_values:
            idx += 2
            continue
        if token.startswith("-"):
            idx += 1
            continue
        # Unknown bare tokens must never silently become remote job
        # submissions (a typo like `slurmech ls` once submitted `ls` as a
        # cluster job); remote commands require an explicit `run --`.
        print(
            f"slurmech: unknown command {token!r}. "
            f"To submit a remote command use: slurmech run -- {' '.join(argv[idx:])}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if idx < len(argv):
        return ["run", *argv[: idx - 1], "--", *argv[idx:]]
    return argv


def main() -> None:
    sys.argv[1:] = _rewrite_bare_command(sys.argv[1:])
    app()
