from __future__ import annotations

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
