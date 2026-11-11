"""The async child runner with real local processes (no CLIs, no APIs)."""

from __future__ import annotations

import asyncio
import os
import signal
import sys

import anyio

from tandem.child import ChildRegistry, run_child
from tandem.state import StatePaths

ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/var/empty")}


def _run(
    argv: list[str],
    *,
    timeout_s: float,
    grace_s: float = 1.0,
    stdin_data: bytes | None = None,
    stdout_cap_bytes: int = 1024 * 1024,
):
    return asyncio.run(
        run_child(
            [sys.executable, *argv],
            env=ENV,
            timeout_s=timeout_s,
            grace_s=grace_s,
            stdout_cap_bytes=stdout_cap_bytes,
            stderr_keep_bytes=64 * 1024,
            stdin_data=stdin_data,
        )
    )


def test_exit_code_passthrough() -> None:
    result = _run(["-c", "import sys; sys.exit(3)"], timeout_s=20)
    assert result.returncode == 3
    assert result.signal is None
    assert not result.timed_out


def test_stdout_stderr_capture_and_first_byte() -> None:
    result = _run(["-c", "import sys; print('out'); print('err', file=sys.stderr)"], timeout_s=20)
    assert result.returncode == 0
    assert result.stdout == b"out\n"
    assert result.stderr == b"err\n"
    assert result.first_byte_s is not None
    assert result.first_byte_s >= 0


def test_stdin_roundtrip() -> None:
    result = _run(
        ["-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        timeout_s=20,
        stdin_data=b"abc",
    )
    assert result.stdout == b"ABC"


def test_hard_timeout_terminates_group() -> None:
    result = _run(["-c", "import time; time.sleep(60)"], timeout_s=0.5, grace_s=0.5)
    assert result.timed_out
    assert result.returncode is not None
    assert result.returncode < 0
    assert result.signal in (signal.SIGTERM.value, signal.SIGKILL.value)


def test_grace_escalation_sigkill() -> None:
    result = _run(
        ["-c", "import signal, time; signal.signal(signal.SIGTERM, lambda *_: None); time.sleep(60)"],
        timeout_s=0.4,
        grace_s=0.4,
    )
    assert result.timed_out
    assert result.signal == signal.SIGKILL.value


def test_stdout_flood_terminates_not_times_out() -> None:
    result = _run(
        ["-c", "for _ in range(400): print('x' * 65536)\nimport time; time.sleep(60)"],
        timeout_s=15,
        grace_s=1.0,
        stdout_cap_bytes=64 * 1024,
    )
    assert result.stdout_truncated
    assert not result.timed_out
    assert len(result.stdout) <= 64 * 1024 + 65536  # cap plus at most one in-flight chunk
    assert result.returncode is not None
    assert result.returncode < 0


def test_ordinary_run_not_truncated() -> None:
    result = _run(["-c", "print('small')"], timeout_s=20, stdout_cap_bytes=64 * 1024)
    assert not result.stdout_truncated
    assert result.stdout == b"small\n"
    assert result.returncode == 0


def test_registry_lifecycle(tmp_path):
    paths = StatePaths(tmp_path / "state")
    paths.ensure_layout()
    registry = ChildRegistry(paths)
    seen: dict[str, object] = {}

    async def scenario() -> None:
        task = asyncio.ensure_future(
            run_child(
                [sys.executable, "-c", "import time; time.sleep(1.0)"],
                env=ENV,
                timeout_s=20,
                grace_s=1.0,
                stdout_cap_bytes=1024 * 1024,
                stderr_keep_bytes=64 * 1024,
                registry=registry,
                run_id="probe-run",
            )
        )
        await asyncio.sleep(0.3)
        await anyio.to_thread.run_sync(_snapshot, registry, seen)
        await task

    asyncio.run(scenario())
    entries = seen["entries"]  # type: ignore[assignment]
    assert len(entries) == 1  # type: ignore[arg-type]
    assert entries[0].run_id == "probe-run"  # type: ignore[index, union-attr]
    assert entries[0].owner_pid == os.getpid()  # type: ignore[index, union-attr]
    assert registry.own() == ()
    assert registry.persistent() == ()


def _snapshot(registry: ChildRegistry, seen: dict[str, object]) -> None:
    seen["entries"] = registry.persistent()
