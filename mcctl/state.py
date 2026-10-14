"""Per cloud/account/signal state layout, manifest and file lock."""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


class LockBusy(RuntimeError):
    pass


def mcctl_home(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("MCCTL_HOME") or Path.home() / ".mcctl")


def _safe(part: str) -> str:
    if not part or not re.fullmatch(r"[A-Za-z0-9._-]+", part):
        raise ValueError(f"unsafe path component: {part!r}")
    return part


@dataclass(frozen=True)
class Layout:
    home: Path
    cloud: str
    account: str
    signal: str

    def __post_init__(self):
        for p in (self.cloud, self.account, self.signal):
            _safe(p)

    @property
    def backend_key(self) -> str:
        return f"state/{self.cloud}/{self.account}/{self.signal}.tfstate"

    @property
    def state_path(self) -> Path:
        return self.home / self.backend_key

    @property
    def workdir(self) -> Path:
        return self.home / "work" / self.cloud / self.account / self.signal

    @property
    def plan_path(self) -> Path:
        return self.workdir / "tfplan"

    @property
    def manifest_path(self) -> Path:
        return self.workdir / "manifest.json"

    @property
    def lock_path(self) -> Path:
        return self.home / "locks" / f"{self.cloud}-{self.account}-{self.signal}.lock"

    def ensure(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Manifest:
    plan_path: str
    plan_sha256: str
    state_serial: int
    summary: dict
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def write_manifest(layout: Layout, m: Manifest) -> None:
    layout.ensure()
    tmp = layout.manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(m), indent=2, sort_keys=True))
    os.replace(tmp, layout.manifest_path)


def read_manifest(layout: Layout) -> Manifest | None:
    if not layout.manifest_path.is_file():
        return None
    return Manifest(**json.loads(layout.manifest_path.read_text()))


@contextmanager
def file_lock(path: Path, timeout: float = 0.0) -> Iterator[None]:
    """Exclusive advisory lock; raises LockBusy if not acquired within timeout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockBusy(f"another mcctl run holds {path}") from None
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)  # closing releases the flock
