"""Marker files are the single source of truth for run states."""

from __future__ import annotations

import posixpath

from slurmech.remote import run_state_from_markers


class MarkerConn:
    def __init__(self, markers: set[str]) -> None:
        self.markers = markers

    def exists(self, path: str) -> bool:
        return posixpath.basename(path) in self.markers


def state_of(*markers: str) -> str:
    return run_state_from_markers(MarkerConn(set(markers)), "/remote/run")


def test_failed_marker_reports_failed() -> None:
    assert state_of(".failed") == "FAILED"


def test_timeout_marker_reports_timeout() -> None:
    assert state_of(".timeout") == "TIMEOUT"


def test_finished_running_pending_and_unknown_states() -> None:
    assert state_of(".finished") == "FINISHED"
    assert state_of(".running") == "RUNNING"
    assert state_of(".pending") == "PENDING"
    assert state_of() == "UNKNOWN"


def test_cancelled_marker_reports_cancelled() -> None:
    assert state_of(".cancelled") == "CANCELLED"


def test_marker_precedence_is_cancelled_timeout_failed_finished() -> None:
    assert state_of(".cancelled", ".timeout", ".failed", ".finished") == "CANCELLED"
    assert state_of(".timeout", ".failed", ".finished") == "TIMEOUT"
    assert state_of(".failed", ".finished", ".running") == "FAILED"
    assert state_of(".finished", ".running") == "FINISHED"
    assert state_of(".running", ".pending") == "RUNNING"
