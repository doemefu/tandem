"""Error taxonomy mapped to MCP ``isError`` results with ``error_kind`` payloads."""

from __future__ import annotations


class TandemError(Exception):
    """Base class for operational tandem failures.

    Every failure surfaced to a calling agent carries an ``error_kind`` so the
    caller can react (stop, retry, tell the user) instead of guessing from prose.
    """

    error_kind = "tandem_error"

    def __init__(self, message: str, *, error_kind: str | None = None) -> None:
        super().__init__(message)
        if error_kind is not None:
            self.error_kind = error_kind

    def payload(self) -> dict[str, object]:
        return {"is_error": True, "error_kind": self.error_kind, "message": str(self)}


class ConfigError(TandemError):
    """Invalid or missing configuration; the server refuses to act."""

    error_kind = "config"


class StateError(TandemError):
    """The shared state directory is unusable."""

    error_kind = "state"


class LockBusyError(TandemError):
    """The cross-instance lock could not be acquired in time."""

    error_kind = "state_lock_busy"


class ChildSpawnError(TandemError):
    """A child CLI could not be spawned."""

    error_kind = "spawn_failed"


class GuardRefusedError(TandemError):
    """Loop guard: tandem was invoked from inside a tandem-spawned child."""

    error_kind = "loop_guard"


class CapExceededError(TandemError):
    """A cross-instance cap (children or daily consults) is exhausted."""

    error_kind = "cap_exceeded"
