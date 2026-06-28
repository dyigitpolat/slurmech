"""Credential resolution for slurmech."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from slurmech.config import WorkspaceConfig


@dataclass(frozen=True)
class Credentials:
    user: str
    host: str
    remote_dir: str
    password: str | None = None
    port: int = 22
    key_filename: str | None = None
    proxy_command: str | None = None
    proxy_host: str | None = None
    proxy_port: int | None = None

    @property
    def routing_mode(self) -> str:
        if self.proxy_command:
            return "proxy-command"
        if self.proxy_host:
            return "tunnel-endpoint"
        return "direct"

    @property
    def connection_host(self) -> str:
        if self.proxy_command:
            return self.host
        return self.proxy_host or self.host

    @property
    def connection_port(self) -> int:
        if self.proxy_command:
            return self.port
        if self.proxy_host:
            return self.proxy_port or 22
        return self.port

    @property
    def display_target(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"

    @property
    def display_route(self) -> str:
        if self.proxy_command:
            return f"proxy-command: {self.proxy_command}"
        if self.proxy_host:
            return f"tunnel endpoint: {self.proxy_host}:{self.connection_port}"
        return "direct"


def parse_env_file(path: Path) -> dict[str, str]:
    values = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def load_credentials(config: WorkspaceConfig) -> Credentials:
    values: dict[str, str] = {}

    profile_env = config.profile_dir / "credentials.env"
    values.update(parse_env_file(profile_env))
    values.update(parse_env_file(config.root / ".env"))

    for key in [
        "REMOTE_USER",
        "REMOTE_HOST",
        "REMOTE_DIR",
        "REMOTE_PASS",
        "REMOTE_PORT",
        "REMOTE_KEY",
        "REMOTE_PROXY_COMMAND",
        "REMOTE_PROXY_HOST",
        "REMOTE_PROXY_PORT",
    ]:
        if os.environ.get(key):
            values[key] = os.environ[key]

    missing = [key for key in ["REMOTE_USER", "REMOTE_HOST", "REMOTE_DIR"] if not values.get(key)]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Missing required slurmech credential values: {joined}")

    return Credentials(
        user=values["REMOTE_USER"],
        host=values["REMOTE_HOST"],
        remote_dir=values["REMOTE_DIR"],
        password=values.get("REMOTE_PASS"),
        port=int(values.get("REMOTE_PORT", "22")),
        key_filename=values.get("REMOTE_KEY"),
        proxy_command=values.get("REMOTE_PROXY_COMMAND"),
        proxy_host=values.get("REMOTE_PROXY_HOST"),
        proxy_port=int(values["REMOTE_PROXY_PORT"]) if values.get("REMOTE_PROXY_PORT") else None,
    )
