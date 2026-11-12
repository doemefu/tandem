"""Run log: JSONL events, naming, permissions, pruning, hashing."""

from __future__ import annotations

import json
import os
from pathlib import Path

from tandem.runlog import RunLog, sha256_hex


def test_event_lines_and_naming(tmp_path: Path) -> None:
    log = RunLog(tmp_path, role="worker", pid=4242)
    log.event("server_started", state_dir="/x", watchdog_pid=99)
    log.event("server_stopped")
    log.close()
    assert log.path.name.endswith("-4242-worker.jsonl")
    records = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert [r["kind"] for r in records] == ["server_started", "server_stopped"]
    assert records[0]["role"] == "worker"
    assert records[0]["watchdog_pid"] == 99
    assert records[0]["ts"] > 0
    assert log.path.stat().st_mode & 0o777 == 0o600


def test_append_creates_missing_dir(tmp_path: Path) -> None:
    runs = tmp_path / "nested" / "runs"
    log = RunLog(runs, role="thinker")
    log.event("server_started")
    log.close()
    assert log.path.exists()
    assert runs.stat().st_mode & 0o777 == 0o700


def test_prune_keeps_newest(tmp_path: Path) -> None:
    names = []
    for i in range(4):
        p = tmp_path / f"{1000 + i}-pid-thinker.jsonl"
        p.write_bytes(b"x" * 50)
        os.utime(p, (1000.0 + i, 1000.0 + i))
        names.append(p.name)
    RunLog.prune(tmp_path, keep_files=2, max_bytes=10_000_000)
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == sorted(names[2:])


def test_sha256_hex_forms_match() -> None:
    assert sha256_hex("tandem") == sha256_hex(b"tandem")
    assert len(sha256_hex("")) == 64
    assert sha256_hex(b"\xff\xfe") != sha256_hex("\xff\xfe")  # str is utf-8 re-encoded
