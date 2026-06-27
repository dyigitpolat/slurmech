# slurmech — Comprehensive Design Plan

## 1. Vision

**slurmech** is a local-first CLI that makes a Slurm cluster feel like an extension of your laptop:

```bash
slurmech python train_cifar.py --epochs 20 --lr 1e-6
```

One command should: detect workspace state → sync code → ensure remote env → submit Slurm job → stream stdout/stderr (and ports) back to the terminal you launched from.

### Design principles

1. **Command-first** — no YAML required for ad-hoc runs; config files for repeatability
2. **Incremental sync** — only changed files per run; shared base via symlinks
3. **Transparent I/O** — stdio and ports routed to local machine
4. **Multi-workspace** — many projects/clusters on one machine under `~/.slurmech/`
5. **Credentials from `.env`** — same ergonomics as current SHAQ workflow
6. **Simpler than slurmster** — drop GUI/grid complexity from v1; add back selectively

### What we keep from slurmster

| slurmster | slurmech |
|-----------|----------|
| Paramiko SSH + SFTP | Same |
| Marker files (`.running`/`.finished`) | Same |
| Local JSON registry | Same, under `~/.slurmech/workspaces/<profile>/runs/` |
| YAML grid experiments | Phase 2 — `slurmech grid config.yaml` |
| FastAPI GUI | Phase 3 — optional |
| Full-dir fetch | Phase 1 — selective fetch by glob |

### What slurmech adds

| Feature | Description |
|---------|-------------|
| **`slurmech <cmd>`** | Arbitrary command, not pre-declared in config |
| **Diff sync** | Per-run overlay dir symlinks stable base |
| **Port forwarding** | `-L 8888` tunnels compute-node ports via login node |
| **`~/.slurmech` profiles** | Multiple workspaces per machine |
| **uv auto-setup** | Detect `pyproject.toml` → `uv sync` on remote |
| **Setup warning** | Clear message + `slurmech init` if remote base missing |
| **Attach / reattach** | `slurmech attach <run_id>` tails log after disconnect |

---

## 2. Configuration model

### 2.1 Directory layout (`~/.slurmech/`)

```
~/.slurmech/
├── config.toml                 # global: default_profile, ssh_options
└── workspaces/
    └── shaq-xlog1/              # profile name = <project>-<host-short>
        ├── workspace.toml        # cluster + slurm + sync settings
        ├── credentials.env       # optional; overrides project .env
        ├── manifest.json         # file hashes from last full sync
        └── runs/
            └── 20250627-153045-a1b2/
                ├── meta.json     # job_id, cmd, state, timestamps
                └── artifacts/    # fetched outputs
```

### 2.2 Project `.slurmech.toml` (optional)

```toml
profile = "shaq-xlog1"

[sync]
include = ["src/**", "legacy/**", "pyproject.toml", "uv.lock"]
exclude = ["**/__pycache__", ".venv", "data/**", "*.pt"]

[slurm]
partition = "gpu"
time = "02:00:00"
gres = "gpu:h100-47:1"
mem = "200G"
cpus_per_gpu = 40

[env]
mode = "uv"                     # uv | script | none
script = "scripts/setup_remote.sh"  # when mode = script
python = "3.12"
```

### 2.3 Credentials resolution order

1. `--profile` → `~/.slurmech/workspaces/<profile>/credentials.env`
2. Project `.env` (`REMOTE_USER`, `REMOTE_HOST`, `REMOTE_DIR`, `REMOTE_PASS`)
3. Environment variables
4. SSH key (no password) if configured in `workspace.toml`

---

## 3. Remote filesystem layout

```
$REMOTE_DIR/                          # e.g. /home/y/yigit/shaq_repro
├── .slurmech/
│   ├── base/                         # last full sync (stable tree)
│   │   ├── src/shaq/...
│   │   ├── pyproject.toml
│   │   └── .venv/ or .uv-venv/       # shared environment
│   ├── manifest.json                 # server-side hash manifest
│   └── runs/
│       └── 20250627-153045-a1b2/     # per-job overlay
│           ├── overlay/              # only changed files (real copies)
│           ├── stdout.log
│           ├── stderr.log
│           ├── .pending | .running | .finished
│           ├── job.slurm.sh
│           └── exitcode
```

### 3.1 Overlay execution model

Job script sets:

```bash
#SBATCH --chdir=$REMOTE_DIR/.slurmech/runs/<run_id>
export SLURMECH_BASE=$REMOTE_DIR/.slurmech/base
export SLURMECH_OVERLAY=$REMOTE_DIR/.slurmech/runs/<run_id>/overlay
export PYTHONPATH="$SLURMECH_OVERLAY:$SLURMECH_BASE:${PYTHONPATH:-}"

# Resolve each file: overlay first, then base
exec slurmech-remote-wrap python train_cifar.py ...
```

`slurmech-remote-wrap` (small bash or Python shim pushed once) resolves paths:

```
for each path component in PYTHONPATH overlay:
  if overlay/foo.py exists → use it
  else → use base/foo.py
```

Symlink alternative (simpler v1):

```bash
mkdir -p $RUN_DIR/workspace
cp -al $BASE/* $RUN_DIR/workspace/     # hardlink farm (fast)
rsync -a $OVERLAY/ $RUN_DIR/workspace/  # overwrite changed files
cd $RUN_DIR/workspace && eval "$CMD"
```

**v1 recommendation:** hardlink + rsync overlay (simpler, no custom path resolver).

---

## 4. Sync pipeline

```
Local project tree
       │
       ▼
[include/exclude glob] ──► file list
       │
       ▼
SHA256 each file ──► compare with manifest.json
       │
       ├── unchanged ──► skip
       └── changed/new ──► upload to overlay/ (run) or base/ (init/full sync)
```

### Commands

| Command | Behavior |
|---------|----------|
| `slurmech init` | First connect; mkdir remote tree; full push to `base/`; run env setup; write manifest |
| `slurmech sync` | Update `base/` with diff only; refresh manifest |
| `slurmech <cmd>` | `sync` if manifest stale (mtime quick-check or `--skip-sync` flag) + create run overlay |

### Setup warning

If remote `$REMOTE_DIR/.slurmech/base/` missing:

```
⚠ Workspace not initialized on xlog1:/home/y/yigit/shaq_repro
  Run: slurmech init
  Or:  slurmech init --env uv   (auto-detect pyproject.toml)
```

---

## 5. Environment setup

### Mode: `uv` (default for Python projects)

Remote `init` runs:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if uv missing
cd $BASE && uv sync --python 3.12
```

Job wrapper activates via:

```bash
source $BASE/.venv/bin/activate   # uv creates .venv by default
```

### Mode: `script`

Run user-provided `scripts/setup_remote.sh` on login node (like current `setup_env.sh`).

### Mode: `reuse`

Point to existing venv path in `workspace.toml`:

```toml
[env]
mode = "reuse"
venv = "/home/y/yigit/imagenetshaq/env"
```

---

## 6. Job lifecycle

```
┌─────────┐   sync/init   ┌──────────┐   sbatch   ┌─────────┐
│  local  │ ────────────► │  login   │ ────────► │  slurm  │
│   CLI   │ ◄── stream ── │   node   │ ◄─ squeue │  compute│
└─────────┘   stdio/port  └──────────┘           └─────────┘
```

1. **Prepare** — resolve profile, credentials, build file diff
2. **Upload** — SFTP overlay files + generated `job.slurm.sh`
3. **Submit** — `sbatch`, parse job ID
4. **Register** — write `runs/<id>/meta.json` locally
5. **Stream** — SSH `tail -F stdout.log` (+ port forwards if requested)
6. **Complete** — detect `.finished` marker or `squeue` gone; record exit code
7. **Fetch** — optional auto-fetch globs from `[fetch]` config

### Generated Slurm script (template)

```bash
#!/bin/bash
#SBATCH --job-name=slurmech-<short-id>
#SBATCH --chdir={run_dir}
#SBATCH --output=stdout.log
#SBATCH --error=stderr.log
# ... directives from workspace.toml ...

touch .pending
trap 'echo $? > exitcode; rm -f .running; touch .finished' EXIT

touch .running
rm -f .pending

# workspace assembly
{assemble_workspace_snippet}

# activate env
{env_snippet}

# user command
{user_cmd}
```

---

## 7. Stdio routing

### stdout/stderr

- Job writes to `stdout.log` / `stderr.log` in run dir (Slurm `#SBATCH --output` also tee'd)
- Local CLI: `tail -F` over SSH persistent connection (reuse slurmster's channel pattern)
- `--detach` skips tail; user runs `slurmech attach <run>`

### Port forwarding

For `slurmech --port 8888 jupyter lab --port 8888`:

1. Parse `--port` flags → list of local:remote pairs (default same port)
2. After job starts, poll run dir for `node.hostname` file written by wrapper:

   ```bash
   echo "$(hostname)" > .compute_node
   ```

3. Open SSH `-L localhost:8888:<compute_node>:8888` through login node
   - Slurm: use `scontrol show job $JOBID` to get NodeList
4. Multiplex: one SSH connection, multiple `-L` forwards

**Constraint:** compute nodes must be reachable from login node (standard HPC).

---

## 8. Python module structure

```
slurmech/
├── cli.py              # typer entry
├── config/
│   ├── global_cfg.py   # ~/.slurmech/config.toml
│   ├── workspace.py    # workspace.toml + .slurmech.toml merge
│   └── credentials.py  # .env loading
├── ssh/
│   ├── connection.py   # Paramiko wrapper (persistent + pool)
│   └── tunnel.py       # port forward management
├── sync/
│   ├── manifest.py     # hash tracking
│   ├── globber.py      # include/exclude
│   └── uploader.py     # SFTP diff push
├── slurm/
│   ├── script.py       # job template rendering
│   ├── submit.py       # sbatch + parse job id
│   └── query.py        # squeue, scontrol, scancel
├── run/
│   ├── registry.py     # local meta.json CRUD
│   ├── lifecycle.py    # pending→running→finished
│   └── stream.py       # tail -F attach
└── remote/
    ├── init.py         # first-time setup
    └── env.py          # uv / script / reuse
```

---

## 9. Implementation phases

### Phase 0 — Scaffold ✅ (this commit)
- [x] Package skeleton, CLI stub, README
- [x] Design doc

### Phase 1 — MVP (2–3 weeks)
- [ ] `credentials.py` — read project `.env`
- [ ] `workspace.py` — profile CRUD under `~/.slurmech/`
- [ ] `connection.py` — Paramiko bash + SFTP
- [ ] `init` — full push + uv sync
- [ ] `slurmech <cmd>` — sync diff, submit, stream stdout
- [ ] Marker files + local registry
- [ ] `status`, `attach`, `cancel`

### Phase 2 — Polish (2 weeks)
- [ ] Port forwarding via `scontrol`
- [ ] `sync` standalone command
- [ ] `[fetch]` patterns + `slurmech fetch`
- [ ] `--reuse` existing remote venv
- [ ] Hardlink overlay assembly benchmarked vs rsync

### Phase 3 — Power features
- [ ] `slurmech grid config.yaml` (port slurmster grid logic)
- [ ] Multi-cluster profiles
- [ ] Array jobs / dependency chains
- [ ] Web dashboard (optional, learn from slurmster GUI)
- [ ] `slurmech shell` — interactive SSH with env activated

### Phase 4 — SHAQ integration
- [ ] shaq-workspace example configs
- [ ] Document repro: `slurmech python legacy/train_cifar.py ...`
- [ ] CI smoke test against mock SSH (pytest + paramiko mock)

---

## 10. SHAQ workspace integration

**shaq-workspace** meta-repo layout:

```
shaq-workspace/
├── .gitmodules
├── shaq/              # submodule — research package
├── slurmech/          # submodule — CLI tool
├── vendor/slurmster/  # submodule — reference only
├── .slurmech.toml     # default profile for this workspace
├── .env.example
└── README.md
```

Example repro command after Phase 1:

```bash
cd shaq-workspace/shaq
slurmech python legacy/train_cifar.py \
  --epochs 20 --lr 1e-6 \
  --enable_shaq 1 --enable_mart 1 --enable_trades 0 \
  --ortho_mode dq --train_with_random 0 --train_with_fgsm 0
```

---

## 11. Additional feature ideas

| Feature | Value |
|---------|-------|
| **`slurmech watch`** | Re-sync on file change (watchfiles) + auto-resubmit dev jobs |
| **`slurmech cost`** | Estimate billing weight from `sacct` TRES |
| **`slurmech doctor`** | Test SSH, Slurm, quota, GPU availability |
| **Run cache** | Skip sync if git HEAD unchanged |
| **Artifact cache** | Local content-addressed store for fetched checkpoints |
| **`--gpu-interactive`** | `srun --pty` instead of batch for debugging |
| **Structured logs** | Parse JSON-lines metrics → local SQLite for experiment tracking |
| **Notifications** | macOS/desktop notify on job complete |

---

## 12. Testing strategy

| Level | What |
|-------|------|
| Unit | manifest diff, glob include/exclude, template rendering, credential resolution |
| Integration | mock SSH server (paramiko transport) for init/sync/submit |
| E2E | manual against xlog1; nightly smoke: `slurmech echo hello` |

---

## 13. Open questions

1. **Compute node SSH** — some clusters block direct node SSH; port forward may require `srun` wrapper
2. **Large artifacts** — git-annex / rclone integration for checkpoints?
3. **Shared base concurrency** — lock file if two `slurmech sync` run simultaneously?
4. **Private slurmech repo?** — public tool, private SHAQ workspace

---

## 14. Success criteria for v1

- [ ] `slurmech init` from shaq repo sets up remote base on xlog1
- [ ] `slurmech python legacy/train_cifar.py ...` reproduces FGSM ~74% / PGD-20 ~38%
- [ ] Disconnect + `slurmech attach` resumes log stream
- [ ] Second run uploads only changed files (<5s sync for single-file edit)
- [ ] Credentials never appear in logs or Slurm scripts
