"""Configuration loading for slurmech."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

from slurmech.registry import workspace_dir


@dataclass(frozen=True)
class SyncConfig:
    include: list[str] = field(default_factory=lambda: ["**/*"])
    exclude: list[str] = field(default_factory=lambda: [".git/**", ".venv/**", "__pycache__/**"])


@dataclass(frozen=True)
class SlurmConfig:
    partition: str | None = None
    time: str = "01:00:00"
    gres: str | None = None
    mem: str | None = None
    cpus_per_gpu: int | None = None


@dataclass(frozen=True)
class EnvConfig:
    mode: str = "uv"
    script: str | None = None
    venv: str | None = None
    python: str | None = None


@dataclass(frozen=True)
class PackDefaults:
    parallelism: int | None = None
    fail_fast: bool = False
    kill_on_failure: bool = False


@dataclass(frozen=True)
class WorkspaceConfig:
    root: Path
    profile: str
    sync: SyncConfig = field(default_factory=SyncConfig)
    slurm: SlurmConfig = field(default_factory=SlurmConfig)
    env: EnvConfig = field(default_factory=EnvConfig)
    pack: PackDefaults = field(default_factory=PackDefaults)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def profile_dir(self) -> Path:
        return workspace_dir(self.profile)


def find_project_config(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for candidate_dir in [current, *current.parents]:
        candidate = candidate_dir / ".slurmech.toml"
        if candidate.exists():
            return candidate
    return None


def _load_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text())


def load_workspace_config(start: Path | None = None, profile: str | None = None) -> WorkspaceConfig:
    config_path = find_project_config(start)
    if config_path is None:
        root = (start or Path.cwd()).resolve()
        raw: dict[str, Any] = {}
    else:
        root = config_path.parent
        raw = _load_toml(config_path)

    selected_profile = profile or raw.get("profile") or root.name
    sync_raw = raw.get("sync", {})
    slurm_raw = raw.get("slurm", {})
    env_raw = raw.get("env", {})
    pack_raw = raw.get("pack", {})

    return WorkspaceConfig(
        root=root,
        profile=selected_profile,
        sync=SyncConfig(
            include=list(sync_raw.get("include", ["**/*"])),
            exclude=list(sync_raw.get("exclude", [".git/**", ".venv/**", "__pycache__/**"])),
        ),
        slurm=SlurmConfig(
            partition=slurm_raw.get("partition"),
            time=slurm_raw.get("time", "01:00:00"),
            gres=slurm_raw.get("gres"),
            mem=slurm_raw.get("mem"),
            cpus_per_gpu=slurm_raw.get("cpus_per_gpu"),
        ),
        env=EnvConfig(
            mode=env_raw.get("mode", "uv"),
            script=env_raw.get("script"),
            venv=env_raw.get("venv"),
            python=env_raw.get("python"),
        ),
        pack=PackDefaults(
            parallelism=pack_raw.get("parallelism"),
            fail_fast=bool(pack_raw.get("fail_fast", False)),
            kill_on_failure=bool(pack_raw.get("kill_on_failure", False)),
        ),
        raw=raw,
    )
