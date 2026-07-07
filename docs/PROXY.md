# Proxy Connections

`slurmech` can run from an SSH/GPU server even when that server cannot directly reach the Slurm login node. The public CLI does not change; only the profile credentials change.

## When To Use This

Use proxy mode when:

- Your laptop or local network can reach the Slurm cluster.
- You are working inside a remote SSH/GPU server, for example through VS Code Remote SSH.
- The SSH/GPU server cannot directly reach the Slurm cluster.
- You still want normal commands such as `slurmech doctor`, `slurmech run`, `slurmech pack`, `slurmech fetch`, and `slurmech attach`.

## Recommended Setup: Persistent Reverse Tunnel (survives editor disconnect)

The GPU server (`sura`) cannot reach `xlog1` directly. Something on your **local network** (laptop, NUS desktop) must keep an outbound SSH session to `sura` with a reverse forward. VS Code / Cursor `RemoteForward` works but **dies when you close the editor** — use a detached `autossh` bridge instead.

### Quick start (local machine)

```bash
# From this repo on your laptop:
./shaq-workspace/slurmech/scripts/xlog1-bridge-local.sh start
./shaq-workspace/slurmech/scripts/xlog1-bridge-local.sh status
```

This appends a `Host sura-xlog1-bridge` entry to `~/.ssh/config` (once) and runs:

```text
autossh -N  sura-xlog1-bridge
  └─ RemoteForward 127.0.0.1:2222 xlog1:22
```

On `sura`, verify anytime:

```bash
./shaq-workspace/slurmech/scripts/xlog1-bridge-check.sh
slurmech doctor
```

**Important:** Remove `RemoteForward 2222 xlog1:22` from your VS Code/Cursor SSH host entry. Only one process can bind `sura:2222`; the standalone bridge and the editor fight for the same port.

### Auto-start on login

#### macOS (launchd)

macOS has no `systemctl`. After running `xlog1-bridge-local.sh start` once (so `~/.ssh/config` has the host block):

```bash
brew install autossh   # if needed
./shaq-workspace/slurmech/scripts/xlog1-bridge-local.sh install-launchd
./shaq-workspace/slurmech/scripts/xlog1-bridge-local.sh status
```

To remove auto-start:

```bash
./shaq-workspace/slurmech/scripts/xlog1-bridge-local.sh uninstall-launchd
```

Check logs: `~/.cache/xlog1-bridge/autossh.log`

**If you see `Bootstrap failed: 125`:** run `install-launchd` from **Terminal.app on your Mac** while logged in locally — not over SSH. SSH sessions have no `gui/` launchd domain. The script now falls back to the `user/` domain automatically when needed.

If you only need the bridge while your laptop is awake (not necessarily after reboot), `./xlog1-bridge-local.sh start` is enough — `autossh -f` already survives closing the terminal.

#### Linux (systemd user service)

After running `xlog1-bridge-local.sh start` once (so `~/.ssh/config` has the host block):

```bash
mkdir -p ~/.config/systemd/user
cp shaq-workspace/slurmech/scripts/xlog1-bridge-local.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now xlog1-bridge-local.service
```

### Manual one-liner (no script)

```bash
autossh -M 0 -f -N \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -R 127.0.0.1:2222:xlog1:22 \
  yigit@sura.ddns.comp.nus.edu.sg
```

### Why this cannot live only on the GPU server

`sura` has no route to `xlog1` (private NUS network). The tunnel must originate from a machine that can reach both endpoints. A persistent bridge is always an **outbound SSH client on the local side**, not a daemon on `sura`.

## Alternative: VS Code Remote SSH Reverse Tunnel (session-bound)

VS Code Remote SSH can also carry the reverse forward, but the tunnel **only lasts while that SSH session is alive**:

```sshconfig
Host gpu-server
  HostName your.gpu.server
  User your_user
  RemoteForward 2222 xlog1:22
  ExitOnForwardFailure yes
  ServerAliveInterval 30
```

While connected, the remote GPU server exposes:

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

1. Confirm the persistent bridge is running on your laptop (`xlog1-bridge-local.sh status`) or the VS Code SSH session is still connected.
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
- If the tunnel is missing, start the persistent bridge on the local machine:

  ```bash
  ./shaq-workspace/slurmech/scripts/xlog1-bridge-local.sh start
  ```

  Or reconnect VS Code Remote SSH (session-bound fallback):

  ```bash
  ssh -N -R 2222:xlog1:22 yigit@sura.ddns.comp.nus.edu.sg
  ```
