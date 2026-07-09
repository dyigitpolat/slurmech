"""Sync manifest must describe the bytes actually uploaded, not pre-upload hashes."""

from pathlib import Path

from slurmech.sync import FileState, build_manifest, changed_files, upload_files


class RecordingConn:
    """Fake SSHConnection capturing put_bytes payloads."""

    def __init__(self):
        self.uploads: dict[str, bytes] = {}

    def put_bytes(self, data: bytes, remote_path: str) -> None:
        self.uploads[remote_path] = data


class MutatingConn(RecordingConn):
    """Mutates the local file the moment it is uploaded (mid-sync editor)."""

    def __init__(self, path: Path, new_content: str):
        super().__init__()
        self._path = path
        self._new_content = new_content

    def put_bytes(self, data: bytes, remote_path: str) -> None:
        super().put_bytes(data, remote_path)
        self._path.write_text(self._new_content)


def test_upload_files_returns_manifest_of_shipped_bytes(tmp_path):
    (tmp_path / "a.txt").write_text("original")
    conn = RecordingConn()

    uploaded = upload_files(conn, tmp_path, [Path("a.txt")], "/remote/base")

    assert conn.uploads["/remote/base/a.txt"] == b"original"
    state = uploaded["a.txt"]
    assert isinstance(state, FileState)
    assert state.size == len(b"original")
    import hashlib
    assert state.sha256 == hashlib.sha256(b"original").hexdigest()


def test_concurrent_edit_during_sync_is_detected_by_next_sync(tmp_path):
    """The poisoning scenario: hash A, upload while file becomes B, revert to A.

    The manifest must record what was SHIPPED so the next sync re-uploads."""
    target = tmp_path / "entries.py"
    target.write_text("state-A")

    files = [Path("entries.py")]
    manifest = build_manifest(tmp_path, files)  # hashes state-A

    # Editor flips the file to state-B just before the upload loop reads it.
    target.write_text("state-B")
    conn = MutatingConn(target, "state-A")  # and back to state-A right after
    shipped = upload_files(conn, tmp_path, files, "/remote/base")

    # Remote holds state-B; local is back to state-A.
    assert conn.uploads["/remote/base/entries.py"] == b"state-B"
    manifest.update(shipped)

    # Next sync: local state-A vs manifest (state-B as shipped) => re-upload.
    next_manifest = build_manifest(tmp_path, files)
    assert changed_files(next_manifest, manifest) == [Path("entries.py")]
