"""Manifest-based workspace sync."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from slurmech.config import WorkspaceConfig
from slurmech.ssh import SSHConnection


@dataclass(frozen=True)
class FileState:
    path: str
    sha256: str
    size: int


Manifest = dict[str, FileState]


def _matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def tracked_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--recurse-submodules"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [path.relative_to(root) for path in root.rglob("*") if path.is_file()]

    return [Path(line) for line in result.stdout.splitlines() if line.strip()]


def _expand_include_pattern(root: Path, pattern: str) -> list[Path]:
    """Expand a sync include glob, including recursive ``**`` patterns."""
    if "**" not in pattern:
        return [path for path in root.glob(pattern) if path.is_file()]

    prefix, _, rest = pattern.partition("**")
    search_root = root / prefix.rstrip("/") if prefix else root
    if not search_root.exists():
        return []

    if rest.startswith("/"):
        rest = rest[1:]
    if rest:
        return [path for path in search_root.rglob(rest) if path.is_file()]
    return [path for path in search_root.rglob("*") if path.is_file()]


def select_files(config: WorkspaceConfig) -> list[Path]:
    candidates = set(tracked_files(config.root))
    for pattern in config.sync.include:
        for path in _expand_include_pattern(config.root, pattern):
            candidates.add(path.relative_to(config.root))

    selected = []
    for rel_path in candidates:
        rel = rel_path.as_posix()
        if not (config.root / rel_path).is_file():
            continue
        if config.sync.include and not _matches_any(rel, config.sync.include):
            continue
        if _matches_any(rel, config.sync.exclude):
            continue
        selected.append(rel_path)
    return sorted(selected)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(root: Path, files: Iterable[Path]) -> Manifest:
    manifest = {}
    for rel_path in files:
        full_path = root / rel_path
        manifest[rel_path.as_posix()] = FileState(
            path=rel_path.as_posix(),
            sha256=sha256_file(full_path),
            size=full_path.stat().st_size,
        )
    return manifest


def manifest_to_json(manifest: Manifest) -> str:
    return json.dumps(
        {path: {"sha256": state.sha256, "size": state.size} for path, state in manifest.items()},
        indent=2,
        sort_keys=True,
    )


def manifest_from_json(text: str) -> Manifest:
    data = json.loads(text) if text.strip() else {}
    return {
        path: FileState(path=path, sha256=value["sha256"], size=value["size"])
        for path, value in data.items()
    }


def changed_files(current: Manifest, previous: Manifest | None) -> list[Path]:
    if previous is None:
        return [Path(path) for path in sorted(current)]
    changed = []
    for path, state in current.items():
        old_state = previous.get(path)
        if old_state is None or old_state.sha256 != state.sha256 or old_state.size != state.size:
            changed.append(Path(path))
    return sorted(changed)


def upload_files(conn: SSHConnection, root: Path, files: Iterable[Path], remote_root: str) -> None:
    for rel_path in files:
        conn.put_file(root / rel_path, f"{remote_root.rstrip('/')}/{rel_path.as_posix()}")


def read_remote_manifest(conn: SSHConnection, remote_manifest: str) -> Manifest | None:
    if not conn.exists(remote_manifest):
        return None
    local_text = conn.sftp().open(remote_manifest).read().decode("utf-8")
    return manifest_from_json(local_text)


def write_remote_manifest(conn: SSHConnection, remote_manifest: str, manifest: Manifest) -> None:
    directory = remote_manifest.rsplit("/", 1)[0]
    conn.mkdirs(directory)
    with conn.sftp().open(remote_manifest, "w") as file:
        file.write(manifest_to_json(manifest))
