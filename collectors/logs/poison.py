"""Shared durable sanitized poison state; no provider SDK dependencies."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


class FilePoisonStore:
    """Durable sanitized attempts/DLQ on an owner-controlled persistent volume.

    Checkpoint follows fsync + atomic replacement. No raw body is stored.
    Filesystem locking protects attempts during overlapping partition ownership.
    """

    def __init__(self, directory: str):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _write(self, path: Path, value: Mapping[str, Any]) -> None:
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix=".poison-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def record_failure(self, identity: str, envelope: Mapping[str, Any]) -> int:
        path = self.directory / f"{identity}.attempt.json"
        fd = os.open(self.directory / ".attempts.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            count = json.loads(path.read_text())["receive_count"] + 1 if path.exists() else 1
            self._write(path, {**envelope, "receive_count": count})
            return count

    def send(self, identity: str, envelope: Mapping[str, Any]) -> None:
        self._write(self.directory / f"{identity}.dlq.json", envelope)

