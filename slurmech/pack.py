"""YAML schema and validation for same-node job packs."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from slurmech.config import PackDefaults


SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class PackChild:
    name: str
    cmd: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PackSpec:
    jobs: list[PackChild]
    parallelism: int
    fail_fast: bool = False
    kill_on_failure: bool = False

    @property
    def child_meta(self) -> list[dict[str, Any]]:
        return [
            {
                "name": child.name,
                "cmd": child.cmd,
                "env": child.env,
                "stdout": f"children/{child.name}/stdout.log",
                "stderr": f"children/{child.name}/stderr.log",
                "exitcode": f"children/{child.name}/exitcode",
            }
            for child in self.jobs
        ]


def _command_to_shell(value: Any) -> str:
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("Pack child command cannot be empty")
        return value
    if isinstance(value, list) and value and all(isinstance(part, (str, int, float)) for part in value):
        return " ".join(shlex.quote(str(part)) for part in value)
    raise ValueError("Pack child command must be a non-empty string or list")


def _validate_env(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Pack child env must be a mapping")
    env = {}
    for key, item in value.items():
        if not isinstance(key, str) or not SAFE_NAME_RE.match(key):
            raise ValueError(f"Unsafe env var name: {key!r}")
        env[key] = str(item)
    return env


def parse_pack_data(data: dict[str, Any], defaults: PackDefaults | None = None) -> PackSpec:
    defaults = defaults or PackDefaults()
    jobs_raw = data.get("jobs")
    if not isinstance(jobs_raw, list) or not jobs_raw:
        raise ValueError("Pack file must contain a non-empty `jobs` list")

    jobs = []
    seen_names = set()
    for idx, raw_job in enumerate(jobs_raw, start=1):
        if not isinstance(raw_job, dict):
            raise ValueError(f"Pack job #{idx} must be a mapping")
        name = raw_job.get("name")
        if not isinstance(name, str) or not SAFE_NAME_RE.match(name):
            raise ValueError(f"Pack job #{idx} has unsafe name: {name!r}")
        if name in seen_names:
            raise ValueError(f"Duplicate pack job name: {name}")
        seen_names.add(name)

        jobs.append(
            PackChild(
                name=name,
                cmd=_command_to_shell(raw_job.get("cmd")),
                env=_validate_env(raw_job.get("env")),
            )
        )

    parallelism = data.get("parallelism", defaults.parallelism or len(jobs))
    if not isinstance(parallelism, int) or parallelism < 1:
        raise ValueError("Pack parallelism must be an integer >= 1")

    return PackSpec(
        jobs=jobs,
        parallelism=min(parallelism, len(jobs)),
        fail_fast=bool(data.get("fail_fast", defaults.fail_fast)),
        kill_on_failure=bool(data.get("kill_on_failure", defaults.kill_on_failure)),
    )


def load_pack_file(path: str | Path, defaults: PackDefaults | None = None) -> PackSpec:
    data = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError("Pack file must be a YAML mapping")
    return parse_pack_data(data, defaults=defaults)
