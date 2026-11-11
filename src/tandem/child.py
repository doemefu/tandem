"""Async runner for tandem-spawned CLI children.

Contract: children run in their own session/process group (``start_new_session``)
so ``os.killpg`` can never hit the server or its own group; termination escalates
SIGTERM -> grace -> SIGKILL against the group and never raises; a kill race (child
exits between timeout and signal) falls through to the collected exit code;
stdout is spooled under a caller-set cap and over-cap drains and terminates the
child as a flood; every exit path reaps and unregisters the child. Cleanup on
cancellation (host gives up) runs shielded so teardown cannot skip the kill.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import signal
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import anyio
import anyio.abc

from tandem.errors import ChildSpawnError, LockBusyError, StateError
from tandem.state import StatePaths, file_lock, load_json, pid_alive, save_json

_CHUNK = 65536
_STALE_AFTER_S = 600.0
_LOCK_TIMEOUT_S = 5.0


def argv_hash(argv: Sequence[str]) -> str:
    """Stable hash of an argv array (never log the raw argv content)."""
    digest = hashlib.sha256()
    for part in argv:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def signal_group(pgid: int, sig: int) -> bool:
    """Signal a process group; False when it no longer exists. Never raises."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@dataclass(frozen=True)
class ChildResult:
    """Outcome of one child run, independent of any CLI-specific parsing."""

    returncode: int | None
    signal: int | None
    timed_out: bool
    stdout: bytes
    stdout_truncated: bool
    stderr: bytes
    stderr_truncated: bool
    duration_s: float
    first_byte_s: float | None


@dataclass(frozen=True)
class RegistryEntry:
    """A live child group; pgid == the child's pid (it is the session leader)."""

    run_id: str
    pgid: int
    started_at: float
    argv_hash: str
    owner_pid: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> RegistryEntry | None:
        try:
            return cls(
                run_id=str(raw["run_id"]),
                pgid=int(raw["pgid"]),
                started_at=float(raw["started_at"]),
                argv_hash=str(raw["argv_hash"]),
                owner_pid=int(raw["owner_pid"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


class ChildRegistry:
    """Live-child registry mirrored to disk under the cross-instance master lock.

    The mirror exists for the watchdog (it kills groups whose owning server
    died) and for ``doctor``. The lock file is the state dir's single master
    lock; it is created once and never unlinked - the kernel releases it when
    the holder dies. Stale entries (dead pgid older than the stale window) are
    pruned opportunistically while the lock is held.  Mutations of the in-memory
    ``_own`` map are serialized by a thread lock; snapshots copy optimistically
    with a retry loop so the signal-handler path never blocks on threading
    primitives.
    """

    def __init__(self, paths: StatePaths) -> None:
        self._paths = paths
        self._own: dict[str, RegistryEntry] = {}
        self._own_lock = threading.Lock()

    def register(self, entry: RegistryEntry) -> None:
        with self._own_lock:
            self._own[entry.run_id] = entry
            snapshot = self._safe_snapshot()
        self._persist(drop=frozenset(), snapshot=snapshot)

    def unregister(self, run_id: str) -> None:
        with self._own_lock:
            self._own.pop(run_id, None)
            snapshot = self._safe_snapshot()
        self._persist(drop=frozenset({run_id}), snapshot=snapshot)

    def own(self) -> tuple[RegistryEntry, ...]:
        """This instance's live entries (no disk access; signal-handler safe)."""
        return self._safe_snapshot()

    def _safe_snapshot(self) -> tuple[RegistryEntry, ...]:
        while True:
            try:
                return tuple(self._own.values())
            except RuntimeError:
                continue  # the dict changed size mid-copy from another thread

    def persistent(self) -> tuple[RegistryEntry, ...]:
        """All instances' live entries, read from the mirror (doctor/watchdog)."""
        data = load_json(self._paths.registry_file, {"children": []})
        if not isinstance(data, dict):
            return ()
        out: list[RegistryEntry] = []
        for raw in data.get("children", []):
            entry = RegistryEntry.from_json(raw)
            if entry is not None:
                out.append(entry)
        return tuple(out)

    def terminate_own(self, sig: int = signal.SIGTERM) -> list[int]:
        """Synchronously signal this instance's live groups; returns signaled pgids."""
        signaled: list[int] = []
        for entry in self.own():
            if signal_group(entry.pgid, sig):
                signaled.append(entry.pgid)
        return signaled

    def _persist(self, *, drop: frozenset[str], snapshot: tuple[RegistryEntry, ...]) -> None:
        now = time.time()
        with file_lock(self._paths.lock, timeout_s=_LOCK_TIMEOUT_S):
            data = load_json(self._paths.registry_file, {"children": []})
            if not isinstance(data, dict):
                data = {"children": []}
            kept: dict[str, dict[str, Any]] = {}
            for raw in data.get("children", []):
                if isinstance(raw, dict):
                    _merge_kept(kept, raw, drop, now)
            for entry in snapshot:
                if entry.run_id not in drop:
                    kept[entry.run_id] = entry.to_json()
            save_json(self._paths.registry_file, {"children": list(kept.values())})


def _merge_kept(kept: dict[str, dict[str, Any]], raw: dict[str, Any], drop: frozenset[str], now: float) -> None:
    run_id = raw.get("run_id")
    if not isinstance(run_id, str) or run_id in drop:
        return
    entry = RegistryEntry.from_json(raw)
    if entry is None:
        return
    if now - entry.started_at > _STALE_AFTER_S and not pid_alive(entry.pgid):
        return
    kept[run_id] = raw


async def run_child(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path | None = None,
    timeout_s: float,
    grace_s: float,
    stdout_cap_bytes: int,
    stderr_keep_bytes: int,
    stdin_data: bytes | None = None,
    registry: ChildRegistry | None = None,
    run_id: str | None = None,
) -> ChildResult:
    """Run one CLI child with full hygiene; see the module docstring."""
    start = time.monotonic()
    try:
        proc = await anyio.open_process([str(part) for part in argv], env=dict(env), cwd=cwd, start_new_session=True)
    except OSError as exc:
        raise ChildSpawnError(f"failed to spawn {argv[0]!r}: {exc}") from exc

    if run_id is None:
        run_id = f"child-{proc.pid}"
    registered = registry is not None
    if registry is not None:
        try:
            await anyio.to_thread.run_sync(
                registry.register,
                RegistryEntry(
                    run_id=run_id,
                    pgid=proc.pid,
                    started_at=time.time(),
                    argv_hash=argv_hash(argv),
                    owner_pid=os.getpid(),
                ),
            )
        except BaseException:
            # A failed registration (lock timeout, registry corruption) must not
            # leave a live, unsupervised child group: kill the fresh group and
            # surface the error to the caller.
            signal_group(proc.pid, signal.SIGTERM)
            with anyio.move_on_after(grace_s, shield=True):
                await proc.wait()
            signal_group(proc.pid, signal.SIGKILL)
            raise

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    flags: dict[str, Any] = {
        "stdout_truncated": False,
        "stderr_truncated": False,
        "timed_out": False,
        "first_byte_s": None,
        "returncode": None,
    }
    flood = anyio.Event()
    exited = anyio.Event()

    def drain(buf: bytearray, chunk: bytes, cap: int) -> bool:
        """Append up to cap; return True when the chunk hit the cap."""
        room = cap - len(buf)
        if room <= 0:
            return True
        buf.extend(chunk[:room])
        return len(chunk) > room

    async def pump(
        stream: anyio.abc.ByteReceiveStream,
        buf: bytearray,
        cap: int,
        truncated_key: str,
        *,
        count_first_byte: bool,
        flood_on_cap: bool,
    ) -> None:
        try:
            while True:
                chunk = await stream.receive(_CHUNK)
                if count_first_byte and flags["first_byte_s"] is None:
                    flags["first_byte_s"] = time.monotonic() - start
                if drain(buf, chunk, cap):
                    flags[truncated_key] = True
                    if flood_on_cap:
                        flood.set()
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            return

    async def pump_stdout() -> None:
        if proc.stdout is not None:
            await pump(
                proc.stdout,
                stdout_buf,
                stdout_cap_bytes,
                "stdout_truncated",
                count_first_byte=True,
                flood_on_cap=True,
            )

    async def pump_stderr() -> None:
        if proc.stderr is not None:
            await pump(
                proc.stderr,
                stderr_buf,
                stderr_keep_bytes,
                "stderr_truncated",
                count_first_byte=False,
                flood_on_cap=False,
            )

    async def write_stdin() -> None:
        if proc.stdin is None:
            return
        try:
            if stdin_data is not None:
                await proc.stdin.send(stdin_data)
        except anyio.BrokenResourceError:
            pass
        finally:
            with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
                await proc.stdin.aclose()

    async def exit_waiter() -> None:
        flags["returncode"] = await proc.wait()
        exited.set()

    async def terminate_watch() -> None:
        """Escalate against the group on flood or hard timeout."""
        async with anyio.create_task_group() as inner:

            async def flood_waiter() -> None:
                await flood.wait()
                inner.cancel_scope.cancel()

            async def timeout_waiter() -> None:
                await anyio.sleep(timeout_s)
                flags["timed_out"] = True
                inner.cancel_scope.cancel()

            inner.start_soon(flood_waiter)
            inner.start_soon(timeout_waiter)
        if flags["returncode"] is not None:
            return
        signal_group(proc.pid, signal.SIGTERM)
        with anyio.move_on_after(grace_s):
            await exited.wait()
        if flags["returncode"] is None:
            signal_group(proc.pid, signal.SIGKILL)

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(pump_stdout)
            tg.start_soon(pump_stderr)
            tg.start_soon(write_stdin)
            tg.start_soon(terminate_watch)
            tg.start_soon(exit_waiter)
            await exited.wait()
            tg.cancel_scope.cancel()
    finally:
        # Cancellation (host gave up) must not leak the child group: kill shielded.
        with anyio.move_on_after(grace_s + 5.0, shield=True):
            if flags["returncode"] is None:
                signal_group(proc.pid, signal.SIGTERM)
                with anyio.move_on_after(min(grace_s, 2.0), shield=True):
                    await exited.wait()
                if flags["returncode"] is None:
                    signal_group(proc.pid, signal.SIGKILL)
        if registered and registry is not None:
            # Off the event loop, shielded: the flock wait inside _persist can
            # stall every concurrent tool call, and a lock failure here must
            # never mask a finished child's result. A leftover entry is inert -
            # the watchdog and the stale prune sweep it.
            def _unregister_quietly() -> None:
                try:
                    registry.unregister(run_id)
                except (OSError, StateError, LockBusyError) as exc:
                    print(f"tandem: registry unregister failed for {run_id}: {exc}", file=sys.stderr)

            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(_unregister_quietly)

    returncode = flags["returncode"]
    return ChildResult(
        returncode=returncode,
        signal=None if returncode is None or returncode >= 0 else -returncode,
        timed_out=bool(flags["timed_out"] or returncode is None),
        stdout=bytes(stdout_buf),
        stdout_truncated=bool(flags["stdout_truncated"]),
        stderr=bytes(stderr_buf),
        stderr_truncated=bool(flags["stderr_truncated"]),
        duration_s=time.monotonic() - start,
        first_byte_s=flags["first_byte_s"],
    )
