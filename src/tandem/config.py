"""Environment-derived configuration and the constructed child environment.

Knobs come from ``TANDEM_*`` variables; unparsable values refuse to start
(strict config). Children never inherit the server environment wholesale: the
constructed environment is an explicit allowlist (PATH/HOME/locale/TLS plus
``TANDEM_*`` minus ``TANDEM_ROLE``) plus ``TANDEM_CHILD=1``, which strips
``VIBE_*`` config overrides and ``VIBE_SESSION`` on the way through.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tandem import ROLES
from tandem.errors import ConfigError

_TRUE = frozenset(("1", "true", "yes", "on"))
_FALSE = frozenset(("0", "false", "no", "off"))
_MIB = 1024 * 1024

# Exact variables passed through to children.
_ENV_EXACT = ("PATH", "HOME", "TMPDIR", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")
# Variable prefixes passed through to children. ANTHROPIC_/CLAUDE_ keep
# operator-chosen routing and auth (API-key gateways, Bedrock/Vertex toggles)
# intact - the env is machine-scoped operator config, not caller input.
_ENV_PREFIXES = ("LANG", "LC_", "ANTHROPIC_", "CLAUDE_")
# Never forwarded, whatever the allowlist says: TANDEM_ROLE would change a nested
# mount's semantics and VIBE_SESSION/VIBE_* would leak host session state and
# config overrides into the child.
_ENV_STRIPPED_EXACT = ("TANDEM_ROLE", "VIBE_SESSION")
_ENV_STRIPPED_PREFIXES = ("VIBE_",)

DEFAULT_STATE_DIR = Path.home() / ".cache" / "tandem"
DEFAULT_MAX_CHILDREN = 2
DEFAULT_DAILY_CONSULT_CAP = 40
DEFAULT_STDOUT_CAP_BYTES = 10 * _MIB
DEFAULT_TASK_TTL_HOURS = 24.0
# A max-length delegation is live for 3600s plus the >=30s kill headroom and
# parse time (~61 min spawn-to-verdict), and the run dir's mtime stays at its
# STAGING time for that whole window (the export lands last).  A TTL sweep
# launched by a concurrent delegation compares against that staging mtime, so
# a TTL at the run's own length can delete a live run's dir.  Two hours keeps
# a comfortable margin and is the floor.
MIN_TASK_TTL_HOURS = 2.0
# The delegation answer a caller sees is capped: MCP payloads that are
# effectively a log dump drown the caller's context.  The full report
# spills to a file in the run dir instead of being lost.
DEFAULT_DELEGATE_RESULT_CAP_BYTES = 30_000
DEFAULT_GRACE_SECONDS = 10.0
DEFAULT_STDERR_KEEP_BYTES = 8 * 1024 * 1024  # per-child stderr accumulation cap


def _env_float_optional(env: Mapping[str, str], name: str, *, minimum: float) -> float | None:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _truthy(raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(raw)


def _env_bool(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return _truthy(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a boolean, got {raw!r}") from exc


def _env_int(env: Mapping[str, str], name: str, *, default: int, minimum: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_float(env: Mapping[str, str], name: str, *, default: float, minimum: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_paths(env: Mapping[str, str], name: str, *, default: tuple[Path, ...]) -> tuple[Path, ...]:
    raw = env.get(name, "")
    if not raw.strip():
        return default
    return tuple(Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip())


@dataclass(frozen=True)
class Config:
    """Validated tandem configuration for one server process."""

    role: str
    state_dir: Path
    cwd: Path
    allowed_roots: tuple[Path, ...]
    max_children: int
    daily_consult_cap: int
    stdout_cap_bytes: int
    task_ttl_hours: float
    grace_seconds: float
    stderr_keep_bytes: int
    delegate_trust: bool
    delegate_max_price: float | None
    delegate_result_cap_bytes: int
    content_log: bool
    claude_bin: str
    vibe_bin: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        role = env.get("TANDEM_ROLE", "")
        if role not in ROLES:
            raise ConfigError(f"refusing to start: TANDEM_ROLE must be one of {', '.join(ROLES)}, got {role!r}")
        state_dir_raw = env.get("TANDEM_LOG_DIR", "")
        state_dir = Path(state_dir_raw).expanduser() if state_dir_raw.strip() else DEFAULT_STATE_DIR
        allowed_roots = _env_paths(env, "TANDEM_ALLOWED_ROOTS", default=(Path.cwd(),))
        _reject_home_roots(allowed_roots)
        return cls(
            role=role,
            state_dir=state_dir,
            cwd=Path.cwd(),
            allowed_roots=allowed_roots,
            max_children=_env_int(env, "TANDEM_MAX_CHILDREN", default=DEFAULT_MAX_CHILDREN, minimum=1),
            daily_consult_cap=_env_int(env, "TANDEM_DAILY_CONSULT_CAP", default=DEFAULT_DAILY_CONSULT_CAP, minimum=1),
            stdout_cap_bytes=_env_int(env, "TANDEM_STDOUT_CAP_BYTES", default=DEFAULT_STDOUT_CAP_BYTES, minimum=1024),
            task_ttl_hours=_env_float(
                env, "TANDEM_TASK_TTL_H", default=DEFAULT_TASK_TTL_HOURS, minimum=MIN_TASK_TTL_HOURS
            ),
            grace_seconds=_env_float(env, "TANDEM_KILL_GRACE_S", default=DEFAULT_GRACE_SECONDS, minimum=0.05),
            stderr_keep_bytes=_env_int(
                env, "TANDEM_STDERR_KEEP_BYTES", default=DEFAULT_STDERR_KEEP_BYTES, minimum=1024
            ),
            delegate_trust=_env_bool(env, "TANDEM_DELEGATE_TRUST", default=True),
            delegate_max_price=_env_float_optional(env, "TANDEM_DELEGATE_MAX_PRICE", minimum=0.01),
            delegate_result_cap_bytes=(
                _env_int(
                    env,
                    "TANDEM_DELEGATE_RESULT_CAP_BYTES",
                    default=DEFAULT_DELEGATE_RESULT_CAP_BYTES,
                    minimum=1024,
                )
            ),
            content_log=_env_bool(env, "TANDEM_LOG_CONTENT", default=False),
            claude_bin=env.get("TANDEM_CLAUDE_BIN", "claude"),
            vibe_bin=env.get("TANDEM_VIBE_BIN", "vibe"),
        )

    def is_cwd_allowed(self, path: Path) -> bool:
        """Check a delegation target against the per-project trust boundary.

        ``path`` must be a real directory (symlink-resolved) under one of the
        allowlisted roots, and must never be the home directory or an ancestor
        of it (delegating to ``~`` or ``/`` delegates every project at once).
        The allowlist IS the trust grant: downstream of choosing a repo, project
        hooks and config are trusted by design.
        """
        real = path.resolve()
        if not real.is_dir():
            return False
        home = _effective_home()
        if real == home or real in home.parents:
            return False
        return any(real == root.resolve() or root.resolve() in real.parents for root in self.allowed_roots)


def _reject_home_roots(roots: tuple[Path, ...]) -> None:
    """An allowlisted root that is home (or its ancestor) re-opens everything."""
    home = _effective_home()
    for root in roots:
        real = root.expanduser().resolve()
        if real == home or real in home.parents:
            raise ConfigError(
                f"refusing to start: TANDEM_ALLOWED_ROOTS entry {root} is the home directory "
                "or an ancestor of it - allowlist projects, not the home tree"
            )


def _effective_home() -> Path:
    """The home directory is_cwd_allowed refuses; injectable for tests."""
    return Path.home().resolve()


def build_child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the constructed environment for a spawned CLI child."""
    base = os.environ if base is None else base
    out: dict[str, str] = {}
    for key, value in base.items():
        if key in _ENV_STRIPPED_EXACT or key.startswith(_ENV_STRIPPED_PREFIXES):
            continue
        if key.startswith(_ENV_PREFIXES) or key in _ENV_EXACT or key.startswith("TANDEM_"):
            out[key] = value
    out["TANDEM_CHILD"] = "1"
    return out
