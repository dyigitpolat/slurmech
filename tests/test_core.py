from pathlib import Path

import pytest

from slurmech.config import load_workspace_config
from slurmech.credentials import load_credentials, parse_env_file
from slurmech.cli import _rewrite_bare_command
from slurmech.pack import load_pack_file, parse_pack_data
from slurmech.registry import Registry, RunRecord
from slurmech.remote import parse_job_id


def test_parse_job_id_from_sbatch_output() -> None:
    assert parse_job_id("Submitted batch job 123456") == "123456"


def test_parse_job_id_rejects_unparseable_output() -> None:
    with pytest.raises(ValueError):
        parse_job_id("sbatch failed")


def test_rewrite_bare_command_preserves_named_subcommands() -> None:
    assert _rewrite_bare_command(["init", "--force"]) == ["init", "--force"]


def test_rewrite_bare_command_to_run_subcommand() -> None:
    assert _rewrite_bare_command(["--time", "00:05:00", "python", "-c", "print(1)"]) == [
        "run",
        "--time",
        "00:05:00",
        "--",
        "python",
        "-c",
        "print(1)",
    ]


def test_load_workspace_config_from_project_file(tmp_path: Path) -> None:
    (tmp_path / ".slurmech.toml").write_text(
        """
profile = "demo"

[sync]
include = ["src/**"]
exclude = ["data/**"]

[slurm]
partition = "gpu"
time = "00:10:00"
gres = "gpu:1"
mem = "16G"
cpus_per_gpu = 4

[env]
mode = "reuse"
venv = "/remote/env"
"""
    )

    config = load_workspace_config(tmp_path)

    assert config.profile == "demo"
    assert config.sync.include == ["src/**"]
    assert config.slurm.partition == "gpu"
    assert config.slurm.cpus_per_gpu == 4
    assert config.env.mode == "reuse"
    assert config.env.venv == "/remote/env"


def test_parse_pack_file_with_string_and_list_commands(tmp_path: Path) -> None:
    pack_file = tmp_path / "jobs.yaml"
    pack_file.write_text(
        """
parallelism: 2
fail_fast: true
jobs:
  - name: a
    cmd: python -c 'print("a")'
    env:
      CUDA_VISIBLE_DEVICES: "0"
  - name: b
    cmd: [python, -c, "print('b')"]
"""
    )

    spec = load_pack_file(pack_file)

    assert spec.parallelism == 2
    assert spec.fail_fast is True
    assert [job.name for job in spec.jobs] == ["a", "b"]
    assert spec.jobs[0].env == {"CUDA_VISIBLE_DEVICES": "0"}
    assert spec.jobs[1].cmd == "python -c 'print('\"'\"'b'\"'\"')'"


def test_parse_pack_rejects_duplicate_or_unsafe_names() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        parse_pack_data({"jobs": [{"name": "a", "cmd": "echo a"}, {"name": "a", "cmd": "echo b"}]})

    with pytest.raises(ValueError, match="unsafe"):
        parse_pack_data({"jobs": [{"name": "../bad", "cmd": "echo bad"}]})


def test_parse_env_file_and_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        """
REMOTE_USER=yigit
REMOTE_HOST=xlog1
REMOTE_DIR=/home/y/yigit
REMOTE_PASS='secret'
"""
    )
    config = load_workspace_config(tmp_path, profile="demo")
    monkeypatch.setenv("REMOTE_HOST", "override-host")

    values = parse_env_file(tmp_path / ".env")
    credentials = load_credentials(config)

    assert values["REMOTE_PASS"] == "secret"
    assert credentials.user == "yigit"
    assert credentials.host == "override-host"
    assert credentials.remote_dir == "/home/y/yigit"
    assert credentials.password == "secret"


def test_credentials_support_reverse_tunnel_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        """
REMOTE_USER=yigit
REMOTE_HOST=xlog1
REMOTE_DIR=/home/y/yigit
REMOTE_PROXY_HOST=127.0.0.1
REMOTE_PROXY_PORT=2222
"""
    )

    credentials = load_credentials(load_workspace_config(tmp_path, profile="demo"))

    assert credentials.routing_mode == "tunnel-endpoint"
    assert credentials.display_target == "yigit@xlog1:22"
    assert credentials.connection_host == "127.0.0.1"
    assert credentials.connection_port == 2222
    assert credentials.display_route == "tunnel endpoint: 127.0.0.1:2222"


def test_credentials_proxy_command_takes_precedence_over_tunnel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in [
        "REMOTE_USER",
        "REMOTE_HOST",
        "REMOTE_DIR",
        "REMOTE_PROXY_COMMAND",
        "REMOTE_PROXY_HOST",
        "REMOTE_PROXY_PORT",
    ]:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        """
REMOTE_USER=yigit
REMOTE_HOST=xlog1
REMOTE_DIR=/home/y/yigit
REMOTE_PROXY_COMMAND=ssh -W xlog1:22 local-gateway
REMOTE_PROXY_HOST=127.0.0.1
REMOTE_PROXY_PORT=2222
"""
    )

    credentials = load_credentials(load_workspace_config(tmp_path, profile="demo"))

    assert credentials.routing_mode == "proxy-command"
    assert credentials.connection_host == "xlog1"
    assert credentials.connection_port == 22
    assert credentials.proxy_command == "ssh -W xlog1:22 local-gateway"


def test_registry_add_find_update(tmp_path: Path) -> None:
    registry = Registry("demo", root=tmp_path / "demo")
    registry.add_run(RunRecord(run_id="run1", cmd=["echo", "hello"], profile="demo"))

    assert registry.find_run(run_id="run1")["cmd"] == ["echo", "hello"]

    registry.update_run(run_id="run1", job_id="123", state="RUNNING")

    updated = registry.find_run(job_id="123")
    assert updated is not None
    assert updated["state"] == "RUNNING"
