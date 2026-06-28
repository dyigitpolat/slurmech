# slurmech

Run **any command** on a Slurm cluster as if it were local.

Inspired by [slurmster](https://github.com/dyigitpolat/slurmster), but simplified around one core idea:

```bash
slurmech python train.py --epochs 20
slurmech -- python -m pytest tests/
slurmech jupyter lab --port 8888   # streams port back to localhost
```

## Status

**v0.1 — design & scaffold.** See [docs/DESIGN.md](docs/DESIGN.md) for the full architecture plan.

## Install (dev)

```bash
cd slurmech
pip install -e ".[dev]"
```

## Configuration

Global state lives under `~/.slurmech/`:

```
~/.slurmech/
├── config.toml              # global defaults
└── workspaces/
    └── shaq-xlog1/          # one profile per project+cluster
        ├── workspace.toml   # remote host, paths, slurm defaults
        ├── credentials.env  # REMOTE_USER, REMOTE_PASS (chmod 600)
        ├── manifest.json    # tracked files + last sync hash
        └── runs/            # local run registry
```

Project root can link a profile via `.slurmech.toml`:

```toml
profile = "shaq-xlog1"
```

Credentials can also come from the project `.env` (same keys as today: `REMOTE_USER`, `REMOTE_HOST`, `REMOTE_DIR`, `REMOTE_PASS`).

If you run `slurmech` from an SSH/GPU server that cannot directly reach the Slurm cluster, configure a proxy or reverse tunnel. See [docs/PROXY.md](docs/PROXY.md).

## Commands (planned)

| Command | Description |
|---------|-------------|
| `slurmech <cmd...>` | Sync workspace, submit job, stream stdio |
| `slurmech init` | First-time remote setup (venv/uv, push files) |
| `slurmech sync` | Push diff to remote base, update manifest |
| `slurmech status` | List runs + Slurm queue |
| `slurmech attach <run>` | Re-attach to a running job's output |
| `slurmech fetch <run>` | Pull artifacts |
| `slurmech cancel <run>` | scancel + update registry |

## License

MIT
