"""Concurrent registry mutations must never clobber each other."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from slurmech.registry import Registry, RunRecord


def _record(run_id: str) -> RunRecord:
    return RunRecord(run_id=run_id, cmd=["echo", run_id], profile="demo")


def test_stale_instance_add_does_not_clobber_earlier_write(tmp_path: Path) -> None:
    root = tmp_path / "demo"
    stale = Registry("demo", root=root)  # loads the (empty) registry now
    fresh = Registry("demo", root=root)

    fresh.add_run(_record("a"))
    stale.add_run(_record("b"))  # must re-read under the lock, not overwrite "a"

    ids = {run["run_id"] for run in Registry("demo", root=root).all_runs()}
    assert ids == {"a", "b"}


def test_stale_instance_update_preserves_other_runs(tmp_path: Path) -> None:
    root = tmp_path / "demo"
    writer = Registry("demo", root=root)
    writer.add_run(_record("a"))
    stale = Registry("demo", root=root)
    writer.add_run(_record("b"))

    stale.update_run(run_id="a", state="RUNNING")

    reread = Registry("demo", root=root)
    assert reread.find_run(run_id="a")["state"] == "RUNNING"
    assert reread.find_run(run_id="b") is not None


def test_parallel_adds_from_many_instances_all_survive(tmp_path: Path) -> None:
    root = tmp_path / "demo"
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            registry = Registry("demo", root=root)
            for j in range(5):
                registry.add_run(_record(f"r{index}-{j}"))
        except BaseException as exc:  # surfaced below; a test thread must not die silently
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    runs = Registry("demo", root=root).all_runs()
    assert len(runs) == 20
    assert len({run["run_id"] for run in runs}) == 20


def test_registry_file_stays_valid_json_and_atomic(tmp_path: Path) -> None:
    root = tmp_path / "demo"
    registry = Registry("demo", root=root)
    registry.add_run(_record("a"))
    registry.update_run(run_id="a", state="FINISHED")

    data = json.loads((root / "runs.json").read_text())
    assert data["runs"][0]["state"] == "FINISHED"
    assert not (root / "runs.json.tmp").exists()
