"""Shutdown choreography and watchdog management.

Host teardown windows are tight: Claude Code gives a stdio server
SIGINT -> SIGTERM (100 ms) -> SIGKILL (400 ms) before closing stdin; Vibe
closes stdin, waits a grace period, then terminates the process tree. The
main-thread signal handlers therefore do the fastest thing first - a
synchronous ``os.killpg`` sweep over this instance's live child groups -
and then re-terminate the process with the signal's default disposition.
The async route (cancel the serve task from a waiter) does not work here:
``run_stdio_async`` blocks in a stdin read that never observes cooperative
cancellation, so the server would swallow the signal and linger until the
host's SIGKILL (verified live). Dying with the default disposition gives
the hosts the signal-dead process they expect inside their window; the
watchdog owns the grace-then-SIGKILL escalation for any child that ignores
the first sweep.
The MCP stdio transport reads stdin itself and shuts the server down on
client EOF; on that path, children are killed in ``run_child``'s shielded
``finally``, so no stdin reader exists here that could steal protocol
bytes from the transport.

The watchdog covers the one case neither mechanism can: the server itself
being SIGKILLed (macOS has no PDEATHSIG). A detached poller watches the
server pid and kills the registry groups owned by that pid.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import anyio

from tandem.child import ChildRegistry

_SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
# Resolved at import time (sync context): the src/ root for the watchdog's PYTHONPATH.
_SRC_ROOT = str(Path(__file__).resolve().parents[1])


def install_shutdown_handlers(registry: ChildRegistry, requested: threading.Event) -> None:
    """Install main-thread signal handlers: synchronous group kill, then die by signal.

    Never registers partial sets: on the first signal the platform refuses,
    installation stops quietly. ``requested`` is still set first so anything
    inspecting the flag (tests, diagnostics) sees that a shutdown was asked
    for.
    """

    def handler(signum: int, frame: object) -> None:
        requested.set()
        registry.terminate_own(signal.SIGTERM)
        # The serve loop cannot be unwound cooperatively (blocking stdin
        # read); re-terminate with the default disposition so the process
        # dies inside the host's teardown window instead of lingering to
        # the SIGKILL, and so the shell exit semantics say "killed by sig".
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in _SHUTDOWN_SIGNALS:
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError):
            return


async def spawn_watchdog(state_dir: Path, *, grace_s: float, poll_s: float) -> int:
    """Spawn the detached watchdog poller for this server process; returns its pid."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(part for part in (_SRC_ROOT, env.get("PYTHONPATH", "")) if part)
    argv = [
        sys.executable,
        "-m",
        "tandem.watchdog",
        "--state-dir",
        str(state_dir),
        "--server-pid",
        str(os.getpid()),
        "--grace-s",
        str(grace_s),
        "--poll-s",
        str(poll_s),
    ]
    proc = await anyio.open_process(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid
