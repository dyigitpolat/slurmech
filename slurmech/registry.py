"""Local run registry under ~/.slurmech."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def slurmech_home() -> Path:
    return Path.home() / ".slurmech"


def workspace_dir(profile: str) -> Path:
    return slurmech_home() / "workspaces" / profile


@dataclass
class RunRecord:
    run_id: str
    cmd: list[str]
    profile: str
    job_id: str | None = None
    state: str = "CREATED"
    remote_run_dir: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Registry:
    def __init__(self, profile: str, root: Path | None = None) -> None:
        self.profile = profile
        self.root = root or workspace_dir(profile)
        self.root.mkdir(parents=True, exist_ok=True)
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "runs.json"
        self._data: dict[str, list[dict[str, Any]]] = {"runs": []}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._data = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            # A crashed writer can leave a truncated file; quarantine it and
            # start from an empty registry instead of bricking every command.
            quarantine = self.path.with_suffix(".json.corrupt")
            self.path.rename(quarantine)
            self._data = {"runs": []}
            self._save()

    def _save(self) -> None:
        # Atomic replace: a writer dying mid-write must never truncate runs.json.
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def add_run(self, run: RunRecord | dict[str, Any]) -> None:
        item = run.to_dict() if isinstance(run, RunRecord) else run
        self._data["runs"].append(item)
        self._save()

    def all_runs(self) -> list[dict[str, Any]]:
        return list(self._data.get("runs", []))

    def find_run(self, run_id: str | None = None, job_id: str | None = None) -> dict[str, Any] | None:
        for run in self.all_runs():
            if run_id and run.get("run_id") == run_id:
                return run
            if job_id and run.get("job_id") == job_id:
                return run
        return None

    def update_run(self, run_id: str | None = None, job_id: str | None = None, **fields: Any) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if run_id and job_id:
            fields["job_id"] = job_id
        for run in self._data["runs"]:
            if (run_id and run.get("run_id") == run_id) or (job_id and run.get("job_id") == job_id):
                run.update(fields)
                run["updated_at"] = now
        self._save()
