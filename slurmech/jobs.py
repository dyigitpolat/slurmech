"""Slurm job rendering and submission."""

from __future__ import annotations

import shlex
import uuid
from dataclasses import dataclass
from datetime import datetime

from slurmech.config import EnvConfig, SlurmConfig
from slurmech.pack import PackSpec
from slurmech.remote import parse_job_id
from slurmech.ssh import SSHConnection


@dataclass(frozen=True)
class RemoteLayout:
    remote_dir: str

    @property
    def root(self) -> str:
        return f"{self.remote_dir.rstrip('/')}/.slurmech"

    @property
    def base(self) -> str:
        return f"{self.root}/base"

    @property
    def manifest(self) -> str:
        return f"{self.root}/manifest.json"

    @property
    def runs(self) -> str:
        return f"{self.root}/runs"

    def run_dir(self, run_id: str) -> str:
        return f"{self.runs}/{run_id}"

    def overlay_dir(self, run_id: str) -> str:
        return f"{self.run_dir(run_id)}/overlay"

    def workspace_dir(self, run_id: str) -> str:
        return f"{self.run_dir(run_id)}/workspace"


def new_run_id() -> str:
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def env_activation(env: EnvConfig) -> str:
    if env.mode == "reuse" and env.venv:
        return f"source {shlex.quote(env.venv.rstrip('/'))}/bin/activate"
    if env.mode == "uv":
        return "uv sync\nsource .venv/bin/activate"
    if env.mode == "script" and env.script:
        return f"bash {shlex.quote(env.script)}"
    return ":"


def sbatch_directives(slurm: SlurmConfig) -> list[str]:
    directives = [f"#SBATCH --time={slurm.time}"]
    if slurm.partition:
        directives.append(f"#SBATCH --partition={slurm.partition}")
    if slurm.gres:
        directives.append(f"#SBATCH --gres={slurm.gres}")
    if slurm.mem:
        directives.append(f"#SBATCH --mem={slurm.mem}")
    if slurm.cpus_per_gpu:
        directives.append(f"#SBATCH --cpus-per-gpu={slurm.cpus_per_gpu}")
    return directives


def render_job_script(
    run_id: str,
    cmd: list[str],
    layout: RemoteLayout,
    slurm: SlurmConfig,
    env: EnvConfig,
) -> str:
    run_dir = layout.run_dir(run_id)
    workspace = layout.workspace_dir(run_id)
    overlay = layout.overlay_dir(run_id)
    command = " ".join(shlex.quote(part) for part in cmd)
    directives = "\n".join(
        [
            f"#SBATCH --job-name=slurmech_{run_id[-8:]}",
            f"#SBATCH --chdir={run_dir}",
            *sbatch_directives(slurm),
        ]
    )
    activate = env_activation(env)
    return f"""#!/bin/bash
{directives}

set -euo pipefail

RUN_DIR={shlex.quote(run_dir)}
BASE={shlex.quote(layout.base)}
OVERLAY={shlex.quote(overlay)}
WORKSPACE={shlex.quote(workspace)}

touch "$RUN_DIR/.running"
rm -f "$RUN_DIR/.pending"

mkdir -p "$WORKSPACE"
cp -al "$BASE"/. "$WORKSPACE"/
if [ -d "$OVERLAY" ]; then
  rsync -a "$OVERLAY"/ "$WORKSPACE"/
fi

cd "$WORKSPACE"
{activate}

set +e
({command}) > "$RUN_DIR/stdout.log" 2> "$RUN_DIR/stderr.log"
exit_code=$?
set -e

echo "$exit_code" > "$RUN_DIR/exitcode"
if [ "$exit_code" -eq 0 ]; then
  touch "$RUN_DIR/.finished"
else
  touch "$RUN_DIR/.failed"
fi
rm -f "$RUN_DIR/.running"
exit "$exit_code"
"""


def _pack_jobs_declarations(spec: PackSpec) -> str:
    lines = []
    for idx, child in enumerate(spec.jobs):
        env_exports = " ".join(
            f"{key}={shlex.quote(value)}" for key, value in sorted(child.env.items())
        )
        command = f"{env_exports} {child.cmd}".strip()
        lines.append(f"JOB_NAMES[{idx}]={shlex.quote(child.name)}")
        lines.append(f"JOB_CMDS[{idx}]={shlex.quote(command)}")
    return "\n".join(lines)


def render_pack_script(
    run_id: str,
    spec: PackSpec,
    layout: RemoteLayout,
    slurm: SlurmConfig,
    env: EnvConfig,
) -> str:
    run_dir = layout.run_dir(run_id)
    workspace = layout.workspace_dir(run_id)
    overlay = layout.overlay_dir(run_id)
    directives = "\n".join(
        [
            f"#SBATCH --job-name=slurmech_pack_{run_id[-8:]}",
            f"#SBATCH --chdir={run_dir}",
            *sbatch_directives(slurm),
        ]
    )
    activate = env_activation(env)
    declarations = _pack_jobs_declarations(spec)
    fail_fast = "1" if spec.fail_fast else "0"
    kill_on_failure = "1" if spec.kill_on_failure else "0"
    return f"""#!/bin/bash
{directives}

set -euo pipefail

RUN_DIR={shlex.quote(run_dir)}
BASE={shlex.quote(layout.base)}
OVERLAY={shlex.quote(overlay)}
WORKSPACE={shlex.quote(workspace)}
PARALLELISM={spec.parallelism}
FAIL_FAST={fail_fast}
KILL_ON_FAILURE={kill_on_failure}

exec > "$RUN_DIR/stdout.log" 2> "$RUN_DIR/stderr.log"

touch "$RUN_DIR/.running"
rm -f "$RUN_DIR/.pending"

mkdir -p "$WORKSPACE" "$RUN_DIR/children"
cp -al "$BASE"/. "$WORKSPACE"/
if [ -d "$OVERLAY" ]; then
  rsync -a "$OVERLAY"/ "$WORKSPACE"/
fi

cd "$WORKSPACE"
{activate}

declare -a JOB_NAMES
declare -a JOB_CMDS
{declarations}

declare -a ACTIVE_PIDS
declare -a ACTIVE_NAMES
overall_exit=0

run_child() {{
  local name="$1"
  local command="$2"
  local child_dir="$RUN_DIR/children/$name"
  mkdir -p "$child_dir"
  echo "starting $name"
  (
    cd "$WORKSPACE"
    bash -lc "$command"
  ) > "$child_dir/stdout.log" 2> "$child_dir/stderr.log"
  local exit_code=$?
  echo "$exit_code" > "$child_dir/exitcode"
  echo "finished $name exit_code=$exit_code"
  return "$exit_code"
}}

kill_active() {{
  for pid in "${{ACTIVE_PIDS[@]:-}}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
}}

wait_one() {{
  local pid="${{ACTIVE_PIDS[0]}}"
  local name="${{ACTIVE_NAMES[0]}}"
  set +e
  wait "$pid"
  local code=$?
  set -e
  if [ "$code" -ne 0 ] && [ "$overall_exit" -eq 0 ]; then
    overall_exit="$code"
  fi
  if [ "$code" -ne 0 ] && [ "$FAIL_FAST" -eq 1 ]; then
    if [ "$KILL_ON_FAILURE" -eq 1 ]; then
      kill_active
    fi
  fi
  echo "$name $code" >> "$RUN_DIR/children/status.tsv"
  ACTIVE_PIDS=("${{ACTIVE_PIDS[@]:1}}")
  ACTIVE_NAMES=("${{ACTIVE_NAMES[@]:1}}")
}}

for idx in "${{!JOB_NAMES[@]}}"; do
  name="${{JOB_NAMES[$idx]}}"
  cmd="${{JOB_CMDS[$idx]}}"
  run_child "$name" "$cmd" &
  ACTIVE_PIDS+=("$!")
  ACTIVE_NAMES+=("$name")

  while [ "${{#ACTIVE_PIDS[@]}}" -ge "$PARALLELISM" ]; do
    wait_one
    if [ "$overall_exit" -ne 0 ] && [ "$FAIL_FAST" -eq 1 ]; then
      break 2
    fi
  done
done

while [ "${{#ACTIVE_PIDS[@]}}" -gt 0 ]; do
  wait_one
done

echo "$overall_exit" > "$RUN_DIR/exitcode"
if [ "$overall_exit" -eq 0 ]; then
  touch "$RUN_DIR/.finished"
else
  touch "$RUN_DIR/.failed"
fi
rm -f "$RUN_DIR/.running"
exit "$overall_exit"
"""


def submit_job(
    conn: SSHConnection,
    run_id: str,
    cmd: list[str],
    layout: RemoteLayout,
    slurm: SlurmConfig,
    env: EnvConfig,
) -> tuple[str, str]:
    run_dir = layout.run_dir(run_id)
    job_script = f"{run_dir}/job.slurm.sh"
    conn.mkdirs(run_dir)
    with conn.sftp().open(job_script, "w") as file:
        file.write(render_job_script(run_id, cmd, layout, slurm, env))
    conn.bash(f"chmod +x {shlex.quote(job_script)} && touch {shlex.quote(run_dir)}/.pending")
    rc, out, err = conn.bash(f"sbatch {shlex.quote(job_script)}")
    if rc != 0:
        raise RuntimeError(f"sbatch failed: {err or out}")
    return parse_job_id(out), out


def submit_pack_job(
    conn: SSHConnection,
    run_id: str,
    spec: PackSpec,
    layout: RemoteLayout,
    slurm: SlurmConfig,
    env: EnvConfig,
) -> tuple[str, str]:
    run_dir = layout.run_dir(run_id)
    job_script = f"{run_dir}/job.slurm.sh"
    conn.mkdirs(run_dir)
    with conn.sftp().open(job_script, "w") as file:
        file.write(render_pack_script(run_id, spec, layout, slurm, env))
    conn.bash(f"chmod +x {shlex.quote(job_script)} && touch {shlex.quote(run_dir)}/.pending")
    rc, out, err = conn.bash(f"sbatch {shlex.quote(job_script)}")
    if rc != 0:
        raise RuntimeError(f"sbatch failed: {err or out}")
    return parse_job_id(out), out
