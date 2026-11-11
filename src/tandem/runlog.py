"""Per-process JSONL run log in the state dir.

Metadata first: by default only kinds, durations, exit codes, sizes, hashes
and error metadata are logged - never prompt or result content. Content
logging via ``TANDEM_LOG_CONTENT`` is a reserved opt-in knob: no tool
applies it yet, and when one does it will be at explicit call sites of
the tool layer, with size caps and redaction.
One file per server process, pruned by count and total bytes (the prune
call lives in ``tandem.__main``).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import IO, Any

from tandem.state import ensure_dir, prune_dir

RUNS_KEEP_FILES = 200
RUNS_MAX_BYTES = 20 * 1024 * 1024


def sha256_hex(data: bytes | str) -> str:
    """Hash content for the audit log; never log the content itself by default."""
    raw = data.encode("utf-8", errors="replace") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


class RunLog:
    """Append-only JSONL events for one server process."""

    def __init__(self, runs_dir: Path, *, role: str, pid: int | None = None) -> None:
        self.runs_dir = runs_dir
        self.role = role
        self.path = runs_dir / f"{int(time.time() * 1000)}-{pid or os.getpid()}-{role}.jsonl"
        self._fh: IO[str] | None = None

    def event(self, kind: str, **fields: Any) -> None:
        record: dict[str, Any] = {"ts": round(time.time(), 3), "kind": kind, "role": self.role}
        record.update(fields)
        if self._fh is None:
            self._open()
        fh = self._fh
        if fh is not None:
            fh.write(json.dumps(record, separators=(",", ":")))
            fh.write("\n")
            fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def _open(self) -> None:
        ensure_dir(self.runs_dir)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._fh = os.fdopen(fd, "a", encoding="utf-8")
        os.chmod(self.path, 0o600)

    @classmethod
    def prune(
        cls,
        runs_dir: Path,
        *,
        keep_files: int = RUNS_KEEP_FILES,
        max_bytes: int = RUNS_MAX_BYTES,
    ) -> None:
        prune_dir(runs_dir, keep_files=keep_files, max_bytes=max_bytes)
