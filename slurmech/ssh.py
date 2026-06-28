"""SSH and SFTP primitives for slurmech."""

from __future__ import annotations

import os
import posixpath
import shlex
from pathlib import Path
from stat import S_ISDIR
from typing import Callable, Iterator

import paramiko


class SSHConnection:
    def __init__(
        self,
        host: str,
        user: str,
        port: int = 22,
        password: str | None = None,
        key_filename: str | None = None,
        connect_host: str | None = None,
        connect_port: int | None = None,
        proxy_command: str | None = None,
    ) -> None:
        self.host = host
        self.user = user
        self.port = port
        self.password = password
        self.key_filename = key_filename
        self.connect_host = connect_host or host
        self.connect_port = connect_port or port
        self.proxy_command = proxy_command
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self._proxy_sock: paramiko.ProxyCommand | None = None

    def __enter__(self) -> "SSHConnection":
        return self.connect()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> "SSHConnection":
        if self._client:
            return self
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        sock = None
        if self.proxy_command:
            sock = paramiko.ProxyCommand(self.proxy_command)
            self._proxy_sock = sock
        client.connect(
            hostname=self.connect_host,
            port=self.connect_port,
            username=self.user,
            password=self.password,
            key_filename=self.key_filename,
            allow_agent=True,
            look_for_keys=True,
            timeout=30,
            sock=sock,
        )
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)
        self._client = client
        self._sftp = client.open_sftp()
        return self

    def close(self) -> None:
        try:
            if self._sftp:
                self._sftp.close()
        finally:
            self._sftp = None
        try:
            if self._client:
                self._client.close()
        finally:
            self._client = None
        try:
            if self._proxy_sock:
                self._proxy_sock.close()
        finally:
            self._proxy_sock = None

    def bash(self, command: str, get_pty: bool = False) -> tuple[int, str, str]:
        if not self._client:
            raise RuntimeError("SSHConnection is not connected")
        cmd = f"bash -lc {shlex.quote(command)}"
        _, stdout, stderr = self._client.exec_command(cmd, get_pty=get_pty)
        out = stdout.read().decode("utf-8", "ignore")
        err = stderr.read().decode("utf-8", "ignore")
        return stdout.channel.recv_exit_status(), out, err

    def stream_tail(self, remote_file: str, from_start: bool = False, lines: int = 100) -> Iterator[str]:
        if not self._client:
            raise RuntimeError("SSHConnection is not connected")
        start = "+1" if from_start else str(max(0, int(lines)))
        cmd = f"tail -n {start} -F {shlex.quote(remote_file)}"
        transport = self._client.get_transport()
        if transport is None:
            raise RuntimeError("No SSH transport")
        channel = transport.open_session()
        channel.get_pty()
        channel.exec_command(f"bash -lc {shlex.quote(cmd)}")
        buffer = b""
        try:
            while True:
                if channel.recv_ready():
                    chunk = channel.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        yield line.decode("utf-8", "ignore")
                if channel.exit_status_ready():
                    if buffer:
                        yield buffer.decode("utf-8", "ignore")
                    break
        finally:
            channel.close()

    def run_with_streaming(self, command: str, stream_callback: Callable[[str], None]) -> int:
        if not self._client:
            raise RuntimeError("SSHConnection is not connected")
        transport = self._client.get_transport()
        if transport is None:
            raise RuntimeError("No SSH transport")
        channel = transport.open_session()
        channel.get_pty()
        channel.exec_command(f"bash -lc {shlex.quote(command)}")
        buffer = b""
        try:
            while True:
                if channel.recv_ready():
                    chunk = channel.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        text = line.decode("utf-8", "ignore")
                        if text.strip():
                            stream_callback(text)
                if channel.exit_status_ready():
                    if buffer:
                        text = buffer.decode("utf-8", "ignore")
                        if text.strip():
                            stream_callback(text)
                    break
            return channel.recv_exit_status()
        finally:
            channel.close()

    def sftp(self) -> paramiko.SFTPClient:
        if not self._sftp:
            raise RuntimeError("SFTP session is not open")
        return self._sftp

    def exists(self, path: str) -> bool:
        try:
            self.sftp().stat(path)
            return True
        except OSError:
            return False

    def isdir(self, path: str) -> bool:
        try:
            return S_ISDIR(self.sftp().stat(path).st_mode)
        except OSError:
            return False

    def mkdirs(self, path: str) -> None:
        parts = [part for part in path.split("/") if part]
        current = "/" if path.startswith("/") else ""
        for part in parts:
            current = posixpath.join(current, part) if current else part
            try:
                self.sftp().mkdir(current)
            except OSError:
                pass

    def put_file(self, local_path: str | Path, remote_path: str) -> None:
        self.mkdirs(posixpath.dirname(remote_path))
        self.sftp().put(str(local_path), remote_path)

    def get_file(self, remote_path: str, local_path: str | Path) -> None:
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self.sftp().get(remote_path, str(local_path))

    def put_dir(self, local_dir: str | Path, remote_dir: str) -> None:
        local_dir = Path(local_dir)
        for root, _, files in os.walk(local_dir):
            for filename in files:
                local_path = Path(root) / filename
                rel = local_path.relative_to(local_dir)
                remote_path = posixpath.join(remote_dir, rel.as_posix())
                self.put_file(local_path, remote_path)
