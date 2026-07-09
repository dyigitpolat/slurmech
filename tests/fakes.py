"""Shared in-memory fakes for tests; no real SSH connections ever."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Callable


class FakeSFTPFile:
    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)

    def __enter__(self) -> "FakeSFTPFile":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def seek(self, pos: int) -> None:
        self._buffer.seek(pos)

    def tell(self) -> int:
        return self._buffer.tell()


class FakeSFTP:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    def open(self, path: str, mode: str = "r") -> FakeSFTPFile:
        if path not in self.files:
            raise OSError(f"No such remote file: {path}")
        return FakeSFTPFile(self.files[path])


class FakeConn:
    """Stands in for slurmech.ssh.SSHConnection in CLI tests."""

    def __init__(
        self,
        files: dict[str, bytes | str] | None = None,
        dirs: set[str] | None = None,
        bash_handler: Callable[[str], tuple[int, str, str] | None] | None = None,
        bash_bytes_handler: Callable[[str], tuple[int, bytes, str]] | None = None,
    ) -> None:
        self.files: dict[str, bytes] = {
            key: value.encode() if isinstance(value, str) else value
            for key, value in (files or {}).items()
        }
        self.dirs = set(dirs or set())
        self.bash_handler = bash_handler
        self.bash_bytes_handler = bash_bytes_handler
        self.bash_calls: list[str] = []
        self.fetched: list[tuple[str, str]] = []
        self.closed = False

    def exists(self, path: str) -> bool:
        if path in self.files or path in self.dirs:
            return True
        prefix = path.rstrip("/") + "/"
        return any(name.startswith(prefix) for name in list(self.files) + list(self.dirs))

    def bash(self, command: str, get_pty: bool = False) -> tuple[int, str, str]:
        self.bash_calls.append(command)
        if self.bash_handler is not None:
            result = self.bash_handler(command)
            if result is not None:
                return result
        return (0, "", "")

    def bash_bytes(self, command: str) -> tuple[int, bytes, str]:
        self.bash_calls.append(command)
        if self.bash_bytes_handler is not None:
            return self.bash_bytes_handler(command)
        return (1, b"", "no bash_bytes handler configured")

    def sftp(self) -> FakeSFTP:
        return FakeSFTP(self.files)

    def get_file(self, remote_path: str, local_path: str | Path) -> None:
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(self.files[remote_path])
        self.fetched.append((remote_path, str(local)))

    def put_file(self, local_path: str | Path, remote_path: str) -> None:
        self.files[remote_path] = Path(local_path).read_bytes()

    def mkdirs(self, path: str) -> None:
        self.dirs.add(path)

    def close(self) -> None:
        self.closed = True
