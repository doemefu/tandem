"""State directory: permissions, atomic writes, flock, counters, sweeps."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tandem.errors import LockBusyError
from tandem.state import (
    RollingCounter,
    StatePaths,
    atomic_write_bytes,
    ensure_dir,
    file_lock,
    load_json,
    pid_alive,
    prune_dir,
    save_json,
    sweep_ttl,
)


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_ensure_dir_permissions(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested"
    ensure_dir(target)
    assert _mode(target) == 0o700
    assert _mode(tmp_path / "deep") == 0o700


def test_atomic_write_roundtrip_and_permissions(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    atomic_write_bytes(target, b'{"a": 1}')
    assert target.read_bytes() == b'{"a": 1}'
    assert _mode(target) == 0o600
    assert not list(tmp_path.glob("*.tmp-*"))


def test_json_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    save_json(target, {"children": [1, 2]})
    assert load_json(target, {}) == {"children": [1, 2]}
    assert load_json(tmp_path / "absent.json", {"default": True}) == {"default": True}


def test_corrupt_state_is_quarantined(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{not json")
    assert load_json(target, {"recovered": True}) == {"recovered": True}
    quarantined = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "{not json"


def test_file_lock_excludes_concurrent_holder(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    script = (
        "import fcntl, sys, time\n"
        "fh = open(sys.argv[1], 'a+')\n"
        "fcntl.flock(fh, fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        "time.sleep(3)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(lock)],
        stdout=subprocess.PIPE,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == b"held"
    with pytest.raises(LockBusyError), file_lock(lock, timeout_s=0.5, poll_s=0.05):
        pass
    holder.wait(timeout=10)
    with file_lock(lock, timeout_s=2.0, poll_s=0.05):
        pass


def test_rolling_counter_window_and_persistence(tmp_path: Path) -> None:
    path = tmp_path / "counters.json"
    counter = RollingCounter(path, window_s=3600.0)
    assert counter.count(now=1000.0) == 0
    assert counter.add(now=1000.0) == 1
    assert counter.add(now=1500.0) == 2
    assert counter.count(now=2000.0) == 2
    assert counter.count(now=2000.0 + 3601.0) == 0  # both calls fell out of the window
    assert counter.remaining(5, now=1500.0) == 3  # cap minus the two calls in the window
    assert counter.add(now=1500.0) == 3
    reopened = RollingCounter(path, window_s=3600.0)
    assert reopened.count(now=1001.0) == 3


def test_prune_dir_limits(tmp_path: Path) -> None:
    for i in range(5):
        p = tmp_path / f"f{i}.dat"
        p.write_bytes(b"x" * 100)
        stamp = 1000.0 + i
        os.utime(p, (stamp, stamp))
    prune_dir(tmp_path, keep_files=3, max_bytes=10_000_000)
    names = {p.name for p in tmp_path.iterdir()}
    assert "f0.dat" not in names
    assert "f1.dat" not in names
    assert len(names) == 3


def test_sweep_ttl(tmp_path: Path) -> None:
    old = tmp_path / "old.dat"
    old.write_bytes(b"x")
    fresh = tmp_path / "fresh.dat"
    fresh.write_bytes(b"x")
    os.utime(old, (time.time() - 100 * 3600,) * 2)
    sweep_ttl(tmp_path, 24.0)
    assert not old.exists()
    assert fresh.exists()


def test_pid_alive_lifecycle() -> None:
    assert pid_alive(os.getpid())
    assert not pid_alive(-1)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    assert not pid_alive(proc.pid)


def test_state_paths_layout(tmp_path: Path) -> None:
    paths = StatePaths(tmp_path / "state")
    paths.ensure_layout()
    for sub in (paths.root, paths.runs_dir, paths.tasks_dir, paths.sessions_dir):
        assert sub.is_dir()
        assert _mode(sub) == 0o700
    assert paths.lock.name == "lock"
    assert paths.registry_file.name == "registry.json"
    assert paths.counters_file.name == "counters.json"
