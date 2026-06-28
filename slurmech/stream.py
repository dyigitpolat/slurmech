"""Remote log streaming helpers."""

from __future__ import annotations

import time
from collections.abc import Callable

from slurmech.remote import squeue_state
from slurmech.ssh import SSHConnection


def stream_stdout_until_done(
    conn: SSHConnection,
    remote_file: str,
    job_id: str,
    emit: Callable[[str], None],
    poll_seconds: float = 2.0,
) -> None:
    offset = 0
    while True:
        if conn.exists(remote_file):
            with conn.sftp().open(remote_file, "rb") as file:
                file.seek(offset)
                data = file.read()
                offset = file.tell()
            if data:
                text = data.decode("utf-8", "ignore")
                for line in text.splitlines():
                    emit(line)

        state = squeue_state(conn, job_id)
        if state is None:
            if conn.exists(remote_file):
                with conn.sftp().open(remote_file, "rb") as file:
                    file.seek(offset)
                    data = file.read()
                if data:
                    for line in data.decode("utf-8", "ignore").splitlines():
                        emit(line)
            return

        time.sleep(poll_seconds)
