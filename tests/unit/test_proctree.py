"""Signal choreography: handlers kill own child groups, then die by signal.

The handler's contract per PR review: kill this instance's registered child
groups synchronously, then re-terminate with the signal's default
disposition (the serve loop sits in a blocking stdin read that ignores
cooperative cancellation - verified live). The kill-path test therefore runs
in a CHILD process that installs the real handlers and registers a
grandchild sleeper; the test signals the child and asserts the child died
by the signal and the sleeper is gone.

The sleepers are spawned with plain ``subprocess.Popen`` and reaped
explicitly: a child spawned through ``asyncio.run()`` keeps its reaping
tied to that event loop's watcher, so a child killed after the loop closes
stays a zombie on Linux's pidfd watcher - and ``pid_alive()`` reports
zombies as alive. Reaping explicitly keeps the tests honest on both macOS
and Linux.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from tandem.child import ChildRegistry, RegistryEntry
from tandem.state import StatePaths, pid_alive

ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}

_SIGNAL_CHILD_SCRIPT = """
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, {src!r})
from tandem.child import ChildRegistry, RegistryEntry
from tandem.proctree import install_shutdown_handlers
from tandem.state import StatePaths

paths = StatePaths(Path(sys.argv[1]))
paths.ensure_layout()
registry = ChildRegistry(paths)

sleeper = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
registry.register(
    RegistryEntry(
        run_id="handler-test",
        pgid=sleeper.pid,
        started_at=time.time(),
        argv_hash="x",
        owner_pid=os.getpid(),
    )
)
print(sleeper.pid, flush=True)

requested = threading.Event()
install_shutdown_handlers(registry, requested)
while True:
    time.sleep(1)
"""


def _spawn_sleeper() -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=ENV,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc


def _reap(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, subprocess.TimeoutExpired):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def _entry(run_id: str, pgid: int) -> RegistryEntry:
    return RegistryEntry(run_id=run_id, pgid=pgid, started_at=time.time(), argv_hash="x", owner_pid=os.getpid())


def test_sigterm_handler_kills_children_then_dies_by_signal(tmp_path: Path) -> None:
    paths = StatePaths(tmp_path / "state")
    paths.ensure_layout()

    script = _SIGNAL_CHILD_SCRIPT.format(src=str(Path(__file__).resolve().parents[2] / "src"))
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(paths.root)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
    )
    sleeper_pid = int(child.stdout.readline().strip())
    try:
        assert pid_alive(sleeper_pid)

        child.send_signal(signal.SIGTERM)
        returncode = child.wait(timeout=5)
        # The handler kills the registered groups, then re-terminates with
        # the default disposition: the process dies by the signal itself.
        assert returncode == -signal.SIGTERM

        deadline = time.monotonic() + 5.0
        while pid_alive(sleeper_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not pid_alive(sleeper_pid)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_terminate_own_signals_only_live_groups(tmp_path: Path) -> None:
    paths = StatePaths(tmp_path / "state")
    paths.ensure_layout()
    registry = ChildRegistry(paths)

    dead = subprocess.Popen(
        [sys.executable, "-c", "pass"], env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    dead.wait(timeout=10)
    live = _spawn_sleeper()

    registry.register(_entry("dead", dead.pid))
    registry.register(_entry("live", live.pid))
    try:
        signaled = registry.terminate_own()
        assert signaled == [live.pid]
        _reap(live)
        assert not pid_alive(live.pid)
    finally:
        registry.unregister("dead")
        registry.unregister("live")
        _reap(live)
