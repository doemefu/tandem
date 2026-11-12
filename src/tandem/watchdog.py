"""Detached watchdog: kills owned child groups when their server process dies.

Rationale (plan v3 review): a macOS ``kqueue``/``EVFILT_PROC`` watcher can only
watch pids that are descendants of the watcher, so a watcher spawned by the
server can never observe the server itself. This module is the out-of-band
replacement the security review required: a plain ``kill(pid, 0)`` liveness
poll loop that, once the owning server pid is gone, SIGTERMs (then SIGKILLs
after the grace period) every registry child group owned by that server - and
only that server - and prunes their entries from the registry.

Run as ``python -m tandem.watchdog --state-dir DIR --server-pid PID``; spawned
detached by the server at startup. Keep this module import-light: it must work
in a bare interpreter with only the tandem package on PYTHONPATH.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tandem.child import signal_group
from tandem.state import StatePaths, file_lock, load_json, pid_alive, save_json


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tandem-watchdog",
        description="Kill tandem child groups when their owning server process dies.",
    )
    parser.add_argument("--state-dir", required=True, help="Shared tandem state directory")
    parser.add_argument("--server-pid", type=int, required=True, help="Server pid to watch")
    parser.add_argument("--grace-s", type=float, default=10.0, help="SIGTERM to SIGKILL grace")
    parser.add_argument("--poll-s", type=float, default=2.0, help="Liveness poll interval")
    args = parser.parse_args(argv if argv is not None else None)

    paths = StatePaths(Path(args.state_dir))
    while pid_alive(args.server_pid):
        time.sleep(args.poll_s)

    owned = _owned_children(paths, args.server_pid)
    live = [entry for entry in owned if pid_alive(int(entry["pgid"]))]
    for entry in live:
        signal_group(int(entry["pgid"]), signal.SIGTERM)

    deadline = time.monotonic() + args.grace_s
    while time.monotonic() < deadline:
        live = [entry for entry in live if pid_alive(int(entry["pgid"]))]
        if not live:
            break
        time.sleep(min(0.2, args.poll_s))
    for entry in live:
        signal_group(int(entry["pgid"]), signal.SIGKILL)

    killed = {entry["run_id"] for entry in owned}
    with file_lock(paths.lock):
        data = load_json(paths.registry_file, {"children": []})
        children = [
            child
            for child in (data.get("children", []) if isinstance(data, dict) else [])
            if child.get("run_id") not in killed
        ]
        save_json(paths.registry_file, {"children": children})
    return 0


def _owned_children(paths: StatePaths, owner_pid: int) -> list[dict[str, Any]]:
    data = load_json(paths.registry_file, {"children": []})
    if not isinstance(data, dict):
        return []
    out: list[dict[str, Any]] = []
    for child in data.get("children", []):
        if isinstance(child, dict) and child.get("owner_pid") == owner_pid:
            out.append(child)
    return out


if __name__ == "__main__":
    sys.exit(main())
