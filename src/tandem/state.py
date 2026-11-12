"""Shared state directory: permissions, atomic writes, flock, JSON persistence.

Security model: the state dir is 0700, files 0600, both set explicitly so the
process umask never widens them. All cross-instance mutations (registry,
counters) happen under one master flock on a single lock file that is never
unlinked - the kernel releases it when the holder dies, so there is no
stale-lock protocol. Corrupt state files are quarantined (renamed aside) and
rebuilt from conservative defaults instead of crashing the server.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tandem.errors import LockBusyError, StateError

LOCK_NAME = "lock"
CORRUPT_SUFFIX = ".corrupt"


def pid_alive(pid: int) -> bool:
    """Return True when ``pid`` exists (signal 0 probe)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else
    return True


def ensure_dir(path: Path) -> None:
    """Create ``path`` (and any missing parents) with 0700 permissions.

    Only directories this call actually creates are chmodded - pre-existing
    ancestors (e.g. ``~/.cache``) keep their own permissions.
    """
    created: list[Path] = []
    current: Path | None = path
    while current is not None and not current.exists():
        created.append(current)
        current = current.parent
    path.mkdir(parents=True, exist_ok=True)
    for entry in created:
        with suppress(OSError):
            os.chmod(entry, 0o700)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Durably write ``data`` to ``path`` with 0600 permissions (tmp + fsync + replace).

    The tmp name carries the pid AND a uuid: two worker threads of one
    process share a pid, and a shared scratch name would race the
    os.replace (one thread consumes it, the other crashes).
    """
    ensure_dir(path.parent)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        with suppress(FileNotFoundError):
            tmp.unlink()


def save_json(path: Path, payload: Any) -> None:
    atomic_write_bytes(path, json.dumps(payload).encode("utf-8"))


def load_json(path: Path, default: Any) -> Any:
    """Load JSON state, quarantining corrupt files and returning ``default``.

    A file that exists but does not parse is renamed to
    ``<name>.corrupt-<ms>`` so the data survives for inspection, then rebuilt
    from the provided default (conservative recovery).
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return default
    except OSError as exc:
        raise StateError(f"unreadable state file {path}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        quarantine = path.with_name(f"{path.name}{CORRUPT_SUFFIX}-{int(time.time() * 1000)}")
        with suppress(OSError):
            os.replace(path, quarantine)
        return default


@contextmanager
def file_lock(lock_path: Path, *, timeout_s: float = 30.0, poll_s: float = 0.1) -> Iterator[int]:
    """Acquire the master flock for ``timeout_s`` at most; never unlink the lock file."""
    ensure_dir(lock_path.parent)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise LockBusyError(f"could not acquire {lock_path} within {timeout_s:.1f}s") from exc
                time.sleep(poll_s)
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def prune_dir(path: Path, *, keep_files: int, max_bytes: int) -> None:
    """Delete oldest files first until under both limits. Never raises."""
    if not path.is_dir():
        return
    try:
        files = [p for p in path.iterdir() if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        files.sort(key=lambda p: p.stat().st_mtime)
        remaining = len(files)
        for entry in files:
            if remaining <= keep_files and total <= max_bytes:
                break
            total -= entry.stat().st_size
            entry.unlink()
            remaining -= 1
    except OSError:
        return


def sweep_ttl(path: Path, ttl_hours: float, *, now: float | None = None) -> None:
    """Delete regular files older than ``ttl_hours``. Never raises."""
    if not path.is_dir():
        return
    now = time.time() if now is None else now
    cutoff = now - ttl_hours * 3600.0
    try:
        for entry in path.iterdir():
            if not entry.is_file():
                continue
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
    except OSError:
        return


@dataclass(frozen=True)
class StatePaths:
    """Well-known paths inside the state dir (all created lazily, 0700)."""

    root: Path

    @property
    def lock(self) -> Path:
        return self.root / LOCK_NAME

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def registry_file(self) -> Path:
        return self.root / "registry.json"

    @property
    def counters_file(self) -> Path:
        return self.root / "counters.json"

    def ensure_layout(self) -> None:
        ensure_dir(self.root)
        ensure_dir(self.runs_dir)
        ensure_dir(self.tasks_dir)
        ensure_dir(self.sessions_dir)


class RollingCounter:
    """Timestamp-window counter persisted as JSON (callers hold the master lock)."""

    def __init__(self, path: Path, *, window_s: float = 86400.0) -> None:
        self.path = path
        self.window_s = window_s

    def _pruned(self, now: float) -> list[float]:
        data = load_json(self.path, {"calls": []})
        calls = data.get("calls", []) if isinstance(data, dict) else []
        return [ts for ts in calls if now - ts <= self.window_s]

    def count(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return len(self._pruned(now))

    def add(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        calls = self._pruned(now)
        calls.append(now)
        save_json(self.path, {"calls": calls})
        return len(calls)

    def remaining(self, cap: int, *, now: float | None = None) -> int:
        return max(0, cap - self.count(now=now))
