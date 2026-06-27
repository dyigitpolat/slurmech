"""CLI entry point for slurmech."""

from __future__ import annotations

import typer

app = typer.Typer(
    name="slurmech",
    help="Run commands on Slurm clusters with workspace sync and live stdio.",
    no_args_is_help=True,
)


@app.command()
def init(
    profile: str = typer.Option(None, "--profile", "-p", help="Workspace profile name"),
    force: bool = typer.Option(False, "--force", help="Re-run remote environment setup"),
) -> None:
    """Initialize remote workspace (first-time setup)."""
    typer.echo("slurmech init — not yet implemented. See docs/DESIGN.md")
    raise typer.Exit(code=1)


@app.command()
def sync(
    profile: str = typer.Option(None, "--profile", "-p"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show diff without uploading"),
) -> None:
    """Sync tracked workspace files to the cluster."""
    typer.echo("slurmech sync — not yet implemented. See docs/DESIGN.md")
    raise typer.Exit(code=1)


@app.command()
def status(
    profile: str = typer.Option(None, "--profile", "-p"),
    all_runs: bool = typer.Option(False, "--all", help="Include fetched/completed runs"),
) -> None:
    """Show run registry and Slurm queue."""
    typer.echo("slurmech status — not yet implemented. See docs/DESIGN.md")
    raise typer.Exit(code=1)


@app.callback(invoke_without_command=True)
def run_command(
    ctx: typer.Context,
    profile: str = typer.Option(None, "--profile", "-p", help="Workspace profile"),
    partition: str = typer.Option(None, "--partition", help="Slurm partition override"),
    gres: str = typer.Option(None, "--gres", help="Slurm GRES override, e.g. gpu:h100-47:1"),
    time: str = typer.Option(None, "--time", help="Slurm time limit, e.g. 02:00:00"),
    port: list[int] = typer.Option(None, "--port", "-L", help="Forward remote port to localhost"),
    detach: bool = typer.Option(False, "--detach", "-d", help="Submit without streaming stdio"),
    cmd: list[str] = typer.Argument(None, help="Command to run remotely"),
) -> None:
    """Run CMD on the Slurm cluster (default when arguments are given)."""
    if ctx.invoked_subcommand is not None:
        return
    if not cmd:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)
    typer.echo(f"Would run remotely: {' '.join(cmd)}")
    typer.echo("slurmech run — not yet implemented. See docs/DESIGN.md")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
