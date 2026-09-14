from __future__ import annotations

from stat import S_IFDIR
from types import SimpleNamespace

import pytest

from slurmech.ssh import SSHConnection


class FakeTransport:
    def __init__(self) -> None:
        self.keepalive = None

    def set_keepalive(self, seconds: int) -> None:
        self.keepalive = seconds


class FakeSFTP:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeSSHClient:
    instances = []

    def __init__(self) -> None:
        self.connect_kwargs = None
        self.transport = FakeTransport()
        self.sftp = FakeSFTP()
        self.closed = False
        self.policy = None
        FakeSSHClient.instances.append(self)

    def set_missing_host_key_policy(self, policy) -> None:
        self.policy = policy

    def connect(self, **kwargs) -> None:
        self.connect_kwargs = kwargs

    def get_transport(self) -> FakeTransport:
        return self.transport

    def open_sftp(self) -> FakeSFTP:
        return self.sftp

    def close(self) -> None:
        self.closed = True


class FakeProxyCommand:
    instances = []

    def __init__(self, command: str) -> None:
        self.command = command
        self.closed = False
        FakeProxyCommand.instances.append(self)

    def close(self) -> None:
        self.closed = True


def test_ssh_connection_uses_tunnel_endpoint(monkeypatch) -> None:
    FakeSSHClient.instances = []
    monkeypatch.setattr("slurmech.ssh.paramiko.SSHClient", FakeSSHClient)

    conn = SSHConnection(
        host="xlog1",
        user="yigit",
        connect_host="127.0.0.1",
        connect_port=2222,
    ).connect()

    kwargs = FakeSSHClient.instances[0].connect_kwargs
    assert kwargs["hostname"] == "127.0.0.1"
    assert kwargs["port"] == 2222
    assert kwargs["username"] == "yigit"
    assert kwargs["sock"] is None

    conn.close()
    assert FakeSSHClient.instances[0].closed is True


def test_bash_bytes_returns_raw_stdout_bytes(monkeypatch) -> None:
    FakeSSHClient.instances = []
    monkeypatch.setattr("slurmech.ssh.paramiko.SSHClient", FakeSSHClient)
    conn = SSHConnection(host="xlog1", user="yigit").connect()

    payload = bytes(range(256))
    captured = {}

    class FakeChannel:
        def recv_exit_status(self) -> int:
            return 0

    class FakeStdout:
        channel = FakeChannel()

        def read(self) -> bytes:
            return payload

    class FakeStderr:
        def read(self) -> bytes:
            return b"warning"

    def exec_command(cmd, get_pty=False):
        captured["cmd"] = cmd
        return (None, FakeStdout(), FakeStderr())

    conn._client.exec_command = exec_command

    rc, out, err = conn.bash_bytes("cd /ws && tar czf - -- generated")

    assert rc == 0
    assert out == payload
    assert err == "warning"
    assert "tar czf - -- generated" in captured["cmd"]
    conn.close()


def test_ssh_connection_uses_paramiko_proxy_command(monkeypatch) -> None:
    FakeSSHClient.instances = []
    FakeProxyCommand.instances = []
    monkeypatch.setattr("slurmech.ssh.paramiko.SSHClient", FakeSSHClient)
    monkeypatch.setattr("slurmech.ssh.paramiko.ProxyCommand", FakeProxyCommand)

    conn = SSHConnection(
        host="xlog1",
        user="yigit",
        proxy_command="ssh -W xlog1:22 gpu-server",
    ).connect()

    proxy = FakeProxyCommand.instances[0]
    kwargs = FakeSSHClient.instances[0].connect_kwargs
    assert proxy.command == "ssh -W xlog1:22 gpu-server"
    assert kwargs["hostname"] == "xlog1"
    assert kwargs["port"] == 22
    assert kwargs["sock"] is proxy

    conn.close()
    assert proxy.closed is True


class FakeDirectorySFTP:
    def __init__(self, directories: set[str], failure: str | None = None) -> None:
        self.directories = set(directories)
        self.failure = failure

    def mkdir(self, path: str) -> None:
        if path in self.directories:
            raise OSError("Failure")
        if path == self.failure:
            raise OSError("Disk quota exceeded")
        self.directories.add(path)

    def stat(self, path: str) -> SimpleNamespace:
        if path not in self.directories:
            raise OSError("No such file")
        return SimpleNamespace(st_mode=S_IFDIR | 0o755)


def test_mkdirs_accepts_only_verified_existing_directory_components() -> None:
    conn = SSHConnection(host="xlog1", user="yigit")
    fake = FakeDirectorySFTP({"/home", "/home/y", "/home/y/yigit"})
    conn._sftp = fake  # type: ignore[assignment]

    conn.mkdirs("/home/y/yigit/.slurmech/runs/new-run/overlay")

    assert "/home/y/yigit/.slurmech/runs/new-run/overlay" in fake.directories


def test_mkdirs_preserves_actual_failed_component_and_server_error() -> None:
    conn = SSHConnection(host="xlog1", user="yigit")
    failed = "/home/y/yigit/.slurmech/runs/new-run"
    fake = FakeDirectorySFTP(
        {
            "/home",
            "/home/y",
            "/home/y/yigit",
            "/home/y/yigit/.slurmech",
            "/home/y/yigit/.slurmech/runs",
        },
        failure=failed,
    )
    conn._sftp = fake  # type: ignore[assignment]

    with pytest.raises(OSError) as captured:
        conn.mkdirs(f"{failed}/overlay/shaq/src")

    message = str(captured.value)
    assert failed in message
    assert f"{failed}/overlay/shaq/src" in message
    assert "Disk quota exceeded" in message
