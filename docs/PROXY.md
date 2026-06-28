# Proxy Connections

`slurmech` can run from an SSH/GPU server even when that server cannot directly reach the Slurm login node. The public CLI does not change; only the profile credentials change.

## When To Use This

Use proxy mode when:

- Your laptop or local network can reach the Slurm cluster.
- You are working inside a remote SSH/GPU server, for example through VS Code Remote SSH.
- The SSH/GPU server cannot directly reach the Slurm cluster.
- You still want normal commands such as `slurmech doctor`, `slurmech run`, `slurmech pack`, `slurmech fetch`, and `slurmech attach`.

## Recommended Setup: VS Code Remote SSH Reverse Tunnel

VS Code Remote SSH uses your normal OpenSSH configuration. Add a reverse forward to the host entry you use for the SSH/GPU server on your local machine:

```sshconfig
Host gpu-server
  HostName your.gpu.server
  User your_user
  RemoteForward 2222 xlog1:22
  ExitOnForwardFailure yes
  ServerAliveInterval 30
```

Then connect to `gpu-server` from VS Code as usual. While that VS Code SSH connection is alive, the remote GPU server exposes:

```text
127.0.0.1:2222 -> xlog1:22
```

On the GPU server, configure `slurmech` with the normal cluster identity plus the local tunnel endpoint:

```bash
REMOTE_USER=yigit
REMOTE_HOST=xlog1
REMOTE_DIR=/home/y/yigit/
REMOTE_PASS=...

REMOTE_PROXY_HOST=127.0.0.1
REMOTE_PROXY_PORT=2222
```

Run the usual commands on the GPU server:

```bash
slurmech doctor
slurmech status --all
slurmech run --time 00:05:00 -- python -c 'print("proxy ok")'
slurmech pack jobs.yaml --detach
```

`slurmech doctor` should show:

```text
target: yigit@xlog1:22
route: tunnel endpoint: 127.0.0.1:2222
```

## Alternative: ProxyCommand

If the GPU server can run an OpenSSH command that reaches a gateway, use:

```bash
REMOTE_USER=yigit
REMOTE_HOST=xlog1
REMOTE_DIR=/home/y/yigit/
REMOTE_PASS=...
REMOTE_PROXY_COMMAND=ssh -W xlog1:22 gateway-host
```

`REMOTE_PROXY_COMMAND` takes precedence over `REMOTE_PROXY_HOST` / `REMOTE_PROXY_PORT`.

## How Routing Works

`REMOTE_HOST` remains the logical Slurm cluster target. Proxy fields only change how the underlying SSH socket is created:

| Mode | Required fields | Actual connection |
|------|-----------------|-------------------|
| Direct | `REMOTE_HOST`, `REMOTE_PORT` | connect to `REMOTE_HOST:REMOTE_PORT` |
| Tunnel endpoint | `REMOTE_PROXY_HOST`, `REMOTE_PROXY_PORT` | connect to tunnel endpoint as the cluster user |
| ProxyCommand | `REMOTE_PROXY_COMMAND` | Paramiko opens an SSH-compatible proxy socket |

All higher-level behavior uses the same connection object, so SFTP sync, stdio attach, fetch, status, cancel, `pack`, and future port forwarding continue to work through the selected route.

## Troubleshooting

If `slurmech doctor` cannot connect:

1. Confirm the VS Code SSH session is still connected.
2. On the GPU server, test the tunnel:

   ```bash
   nc -vz 127.0.0.1 2222
   ```

3. Ensure your local SSH server allowed the reverse forward. `ExitOnForwardFailure yes` makes failures explicit.
4. Confirm no other process on the GPU server is using the chosen reverse port.
5. Try a different port, for example `2223`, and update `REMOTE_PROXY_PORT`.

## Notes For Remote Agents

When running inside the GPU server:

- Do not change `slurmech` commands. Use the same CLI.
- Check `.env` or `~/.slurmech/workspaces/<profile>/credentials.env` for `REMOTE_PROXY_HOST` / `REMOTE_PROXY_PORT`.
- Use `slurmech doctor` before submitting jobs.
- If the tunnel is missing, ask the local user to reconnect VS Code Remote SSH or run:

  ```bash
  ssh -N -R 2222:xlog1:22 gpu-server
  ```
