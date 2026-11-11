"""Tandem's continuation registry for Claude sessions.

Claude sessions persist on Anthropic's side for far longer than this
registry keeps them; the 24h TTL here is a deliberate server policy, not
a limit of Claude itself.  Once a session id has fallen out of the
registry the caller is expected to start a fresh consult - the
``unknown_session`` error copy says exactly that.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anyio import to_thread

from tandem.state import StatePaths, file_lock, load_json, save_json, sweep_ttl

SESSION_TTL_S = 24 * 60 * 60.0

# Ids come back to us from callers (possibly invented by a model), and the
# same id doubles as a filename: the length bound and charset also make
# path traversal via ``..`` or separators impossible, and the leading
# character class keeps an id from looking like a CLI flag downstream.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")


def valid_session_id(value: str) -> bool:
    return isinstance(value, str) and bool(SESSION_ID_RE.fullmatch(value))


@dataclass(frozen=True)
class SessionRecord:
    """What tandem remembers about one Claude session.

    ``cwd`` is the directory the consult ran in.  The probed claude CLI
    (2.1.322) resolves ``--resume`` ids independently of the working
    directory, so the pin is not what makes resume work - it keeps the
    child environment identical across a thread and insures followups
    against future CLI versions that bind transcripts to the project
    directory again.  The thinker re-validates the stored cwd against
    the current allowlist on every followup.
    """

    session_id: str
    created: float
    last_used: float
    consults: int
    cumulative_cost_usd: float | None
    cwd: str


def _record_path(sessions_dir: Path, session_id: str) -> Path:
    return sessions_dir / f"{session_id}.json"


def _parse_record(session_id: str, data: Any) -> SessionRecord | None:
    if not isinstance(data, dict):
        return None
    try:
        raw_cost = data["cumulative_cost_usd"]
        cost = None if raw_cost is None else float(raw_cost)
        cwd = data["cwd"]
        if not isinstance(cwd, str) or not cwd:
            return None  # A record without a resumable cwd cannot continue a session.
        return SessionRecord(
            session_id=session_id,
            created=float(data["created"]),
            last_used=float(data["last_used"]),
            consults=int(data["consults"]),
            cumulative_cost_usd=cost,
            cwd=cwd,
        )
    except (KeyError, TypeError, ValueError):
        return None  # A corrupt record behaves exactly like an unknown session.


def _record_to_json(record: SessionRecord) -> dict[str, Any]:
    return {
        "session_id": record.session_id,
        "created": record.created,
        "last_used": record.last_used,
        "consults": record.consults,
        "cumulative_cost_usd": record.cumulative_cost_usd,
        "cwd": record.cwd,
    }


class SessionRegistry:
    """Persistent continuation records, one 0600 JSON file per session under the master lock."""

    def __init__(self, paths: StatePaths, *, ttl_s: float = SESSION_TTL_S) -> None:
        self._paths = paths
        self._ttl_s = ttl_s

    def get(self, session_id: str, *, now: float | None = None) -> SessionRecord | None:
        """Return the live record for an id; None when unknown, expired or corrupt."""
        if not valid_session_id(session_id):
            return None
        timestamp = time.time() if now is None else now
        with file_lock(self._paths.lock):
            data = load_json(_record_path(self._paths.sessions_dir, session_id), None)
        record = _parse_record(session_id, data)
        if record is None or timestamp - record.last_used > self._ttl_s:
            return None
        return record

    def upsert(
        self,
        session_id: str,
        *,
        cumulative_cost_usd: float | None,
        cwd: str,
        now: float | None = None,
    ) -> SessionRecord:
        """Create or refresh a record; the consults count advances automatically on refresh."""
        if not valid_session_id(session_id):
            msg = f"refusing to store invalid session id {session_id!r}"
            raise ValueError(msg)
        if not cwd:
            msg = "refusing to store a session without a resumable cwd"
            raise ValueError(msg)
        timestamp = time.time() if now is None else now
        with file_lock(self._paths.lock):
            path = _record_path(self._paths.sessions_dir, session_id)
            previous = _parse_record(session_id, load_json(path, None))
            if previous is not None and timestamp - previous.last_used > self._ttl_s:
                previous = None  # expired: a fresh consult, not a continuation
            record = SessionRecord(
                session_id=session_id,
                created=previous.created if previous else timestamp,
                last_used=timestamp,
                consults=(previous.consults + 1) if previous else 1,
                cumulative_cost_usd=cumulative_cost_usd,
                cwd=cwd,
            )
            save_json(path, _record_to_json(record))
            # Retrofit hygiene for a directory that is always small:
            sweep_ttl(self._paths.sessions_dir, self._ttl_s / 3600.0, now=timestamp)
        return record


async def get_session(registry: SessionRegistry, session_id: str) -> SessionRecord | None:
    """Thread-offloaded ``SessionRegistry.get`` for async callers."""
    return await to_thread.run_sync(registry.get, session_id)


async def upsert_session(
    registry: SessionRegistry, session_id: str, *, cumulative_cost_usd: float | None, cwd: str
) -> SessionRecord:
    """Thread-offloaded ``SessionRegistry.upsert`` for async callers."""
    return await to_thread.run_sync(
        lambda: registry.upsert(session_id, cumulative_cost_usd=cumulative_cost_usd, cwd=cwd)
    )
