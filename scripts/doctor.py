"""tandem doctor: environment, mount and state checks for a tandem install.

Usage:
    uv run python scripts/doctor.py

Exit codes: 0 = healthy (warnings allowed), 1 = a check that makes tandem
unusable on this machine failed (tandem itself unreachable, state dir
broken, a mount configured so the server would refuse to start). Missing
CLIs or mounts are warnings, not failures: a machine may legitimately host
only one of the two roles.

The doctor is read-only: it never writes state, mounts, or configs, and
it never spawns claude/vibe beyond a bounded ``--version`` probe. Mounted
``TANDEM_ALLOWED_ROOTS`` are re-validated through the server's own rule
(``tandem.config._reject_home_roots``) so a mount that would fail at
server start surfaces as a failure here. When both mounts are present,
their roots grants are compared against each other; the invoking shell's
``TANDEM_ALLOWED_ROOTS`` is only advisory (it is normally unset there -
the mounts carry the roots, not the operator's environment).
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from importlib.metadata import version as pkg_version
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

# The vibe host kills slow MCP tools at its own tool timeout (~60s default) -
# far under the 1800s a full-length consult_claude may run.  install.sh pins
# these ceilings into the mount; the doctor re-checks what is really written
# (1860 = 1800s consult ceiling + 60s headroom for the result tail).
MIN_TOOL_TIMEOUT_SEC = 1860
MIN_STARTUP_TIMEOUT_SEC = 30

FAILURES = 0
WARNINGS = 0

# The consult window the counter itself uses - imported, not duplicated.
from tandem.state import ROLLING_WINDOW_S  # noqa: E402


def ok(message: str) -> None:
    print(f"[ok]    {message}")


def warn(message: str) -> None:
    global WARNINGS
    WARNINGS += 1
    print(f"[warn]  {message}")


def fail(message: str) -> None:
    global FAILURES
    FAILURES += 1
    print(f"[FAIL]  {message}")


def info(message: str) -> None:
    print(f"[info]  {message}")


def probe(binary: str, timeout_s: float = 10.0) -> str | None:
    """One bounded ``--version`` probe; None on any problem."""
    path = shutil.which(binary)
    if path is None:
        return None
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    first = result.stdout.strip().splitlines()
    return first[0].strip() if first else None


def check_python_and_tandem() -> None:
    ok(f"python {sys.version_info.major}.{sys.version_info.minor}")
    try:
        from tandem import __version__

        ok(f"tandem importable, version {__version__}")
    except Exception as exc:  # pragma: no cover - diagnostics script
        fail(f"tandem not importable from {REPO / 'src'}: {exc}")
    venv_python = REPO / ".venv" / "bin" / "python"
    if venv_python.exists():
        ok(f"venv python present ({venv_python})")
    else:
        warn(f"no venv python at {venv_python} - run `uv sync` before mounting")


def check_clis(vibe_bin: str, claude_bin: str) -> tuple[str | None, str | None]:
    vibe_version = probe(vibe_bin)
    if vibe_version:
        ok(f"vibe on PATH: {vibe_version}")
    else:
        warn(f"vibe not usable on PATH ({vibe_bin!r}) - consults inside Vibe unavailable")
    claude_version = probe(claude_bin)
    if claude_version:
        ok(f"claude on PATH: {claude_version}")
    else:
        warn(f"claude not usable on PATH ({claude_bin!r}) - consults/delegations to Claude Code unavailable")
    return vibe_version, claude_version


def _check_mount_entry(entry: dict, *, role: str, config_name: str, state_dir: Path) -> dict | None:
    """Shared mount checks (role, state dir); returns the mounted env on success."""
    env = entry.get("env")
    env = env if isinstance(env, dict) else {}
    mounted_role = env.get("TANDEM_ROLE", "")
    if mounted_role != role:
        warn(f"{config_name} mount role is {mounted_role!r}, expected {role!r}")
    else:
        ok(f"{config_name} mount present (role {role})")
    mounted_state = env.get("TANDEM_LOG_DIR", "")
    if mounted_state:
        if Path(mounted_state).expanduser() != state_dir:
            warn(f"{config_name} mount state dir {mounted_state!r} differs from the checked state dir {state_dir}")
    else:
        warn(f"{config_name} mount has no TANDEM_LOG_DIR - it resolves to a different default at server start")
    return env


def _check_mounted_timeouts(entry: dict) -> None:
    """Warn when the vibe host's own timeouts would cut tandem's ceilings.

    vibe's host-side tool timeout defaults to ~60s - well under the 1800s a
    ``consult_claude`` may legitimately run.  ``install.sh`` pins
    ``tool_timeout_sec`` (1860 = 1800s consult ceiling + headroom) and
    ``startup_timeout_sec`` (30) into the mount; the doctor verifies what is
    actually written in the config.
    """
    tool_timeout = entry.get("tool_timeout_sec")
    if isinstance(tool_timeout, (int, float)) and not isinstance(tool_timeout, bool):
        if tool_timeout < MIN_TOOL_TIMEOUT_SEC:
            warn(
                f"vibe mount tool_timeout_sec {tool_timeout:g} sits under the {MIN_TOOL_TIMEOUT_SEC}s"
                " a full-length consult needs - re-run scripts/install.sh"
            )
        else:
            ok(f"vibe mount tool_timeout_sec {tool_timeout:g} covers the 1800s consult ceiling")
    else:
        warn("vibe mount has no tool_timeout_sec - the ~60s host default cuts consults; re-run scripts/install.sh")
    startup_timeout = entry.get("startup_timeout_sec")
    if isinstance(startup_timeout, (int, float)) and not isinstance(startup_timeout, bool):
        if startup_timeout < MIN_STARTUP_TIMEOUT_SEC:
            warn(
                f"vibe mount startup_timeout_sec {startup_timeout:g} below {MIN_STARTUP_TIMEOUT_SEC}s"
                " - cold venv-python starts may be cut"
            )
        else:
            ok(f"vibe mount startup_timeout_sec {startup_timeout:g}")
    else:
        warn("vibe mount has no startup_timeout_sec - the host default applies; re-run scripts/install.sh")


def read_vibe_mount(state_dir: Path) -> dict | None:
    """Return the vibe mount's env (or None when there is no usable mount)."""
    config_path = Path.home() / ".vibe" / "config.toml"
    if not config_path.is_file():
        warn(f"no vibe config at {config_path} - thinker mount missing")
        return None
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        warn(f"cannot parse {config_path}: {exc}")
        return None
    entry = _vibe_tandem_entry(config)
    if not isinstance(entry, dict):
        warn("vibe config has no mcp_servers entry named tandem - thinker mount missing (run scripts/install.sh)")
        return None
    _check_mounted_timeouts(entry)
    return _check_mount_entry(entry, role="thinker", config_name="vibe", state_dir=state_dir)


def _vibe_tandem_entry(config: dict) -> object:
    """vibe 3.1.1 writes ``[[mcp_servers]]`` (named array of tables); the
    keyed-table form is tolerated in case future versions change the file."""
    servers = config.get("mcp_servers")
    if isinstance(servers, list):
        for candidate in servers:
            if isinstance(candidate, dict) and candidate.get("name") == "tandem":
                return candidate
        return None
    if isinstance(servers, dict):
        return servers.get("tandem")
    return None


def read_claude_mount(state_dir: Path) -> dict | None:
    """Return the claude mount's env (or None when there is no usable mount)."""
    config_path = Path.home() / ".claude.json"
    if not config_path.is_file():
        warn(f"no claude config at {config_path} - worker mount missing")
        return None
    try:
        config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"cannot parse {config_path}: {exc}")
        return None
    entry = (config.get("mcpServers") or {}).get("tandem")
    if not isinstance(entry, dict):
        warn("claude config has no mcpServers.tandem - worker mount missing (run scripts/install.sh)")
        return None
    return _check_mount_entry(entry, role="worker", config_name="claude", state_dir=state_dir)


def check_mount_roots(vibe_env: dict | None, claude_env: dict | None, operator_roots: str | None) -> None:
    """Roots grants: server-rule validation per mount, mount-vs-mount drift.

    ``operator_roots`` is the invoking shell's ``TANDEM_ALLOWED_ROOTS`` -
    advisory only. Warnings compare roots sources; failures use the
    server's own predicate so install and runtime cannot drift.
    """
    from tandem.config import _reject_home_roots

    for name, env in (("vibe", vibe_env), ("claude", claude_env)):
        if env is None:
            continue
        raw = env.get("TANDEM_ALLOWED_ROOTS", "")
        if not str(raw).strip():
            warn(f"{name} mount has no TANDEM_ALLOWED_ROOTS - the server would default to its cwd at start")
            continue
        try:
            _reject_home_roots(tuple(Path(part).expanduser() for part in str(raw).split(os.pathsep) if part.strip()))
        except Exception as exc:  # ConfigError: the mount would refuse to start
            fail(f"{name} mount roots invalid: {exc}")
    if vibe_env is not None and claude_env is not None:
        vibe_roots = str(vibe_env.get("TANDEM_ALLOWED_ROOTS", ""))
        claude_roots = str(claude_env.get("TANDEM_ALLOWED_ROOTS", ""))
        if vibe_roots != claude_roots:
            warn(f"vibe and claude mounts grant different roots: {vibe_roots!r} vs {claude_roots!r}")
    if operator_roots is not None:
        for name, env in (("vibe", vibe_env), ("claude", claude_env)):
            if env is not None and str(env.get("TANDEM_ALLOWED_ROOTS", "")) != operator_roots:
                warn(f"{name} mount roots differ from this shell's TANDEM_ALLOWED_ROOTS {operator_roots!r}")
    else:
        mounted = next(
            (str(env.get("TANDEM_ALLOWED_ROOTS", "")) for env in (vibe_env, claude_env) if env is not None),
            None,
        )
        if mounted is not None:
            info(f"mounted roots grant: {mounted!r} (this shell sets no TANDEM_ALLOWED_ROOTS)")


def _probe_state_lock(state_dir: Path) -> None:
    """Probe the master lock: the real lock file when it exists, else a fallback."""
    lock_path = state_dir / "lock"
    if lock_path.exists():
        try:
            fd = os.open(lock_path, os.O_RDONLY)
        except OSError as exc:
            fail(f"cannot open master lock {lock_path}: {exc}")
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            warn(f"master lock {lock_path} is currently held (a tandem server is running)")
            return
        finally:
            os.close(fd)
        ok(f"master lock {lock_path} acquirable")
        return
    try:
        from tandem.state import StatePaths, file_lock

        _ = StatePaths(state_dir)
        with tempfile.TemporaryDirectory() as probe_dir:
            probe_lock = Path(probe_dir) / "lock-probe"
            with file_lock(probe_lock, timeout_s=5.0):
                pass
        ok("flock supported (state has no lock file yet - nothing to probe)")
    except Exception as exc:  # pragma: no cover - diagnostics script
        fail(f"lock probe failed: {exc}")


def _consult_count(state_dir: Path, cap: int, *, cap_note: str) -> None:
    """Rolling consult counter - read-only (no quarantine of corrupt files)."""
    counter_path = state_dir / "counters.json"
    if not counter_path.is_file():
        info(f"consults_last_24h: 0/{cap} ({cap_note}; no counters file yet)")
        return
    try:
        data = json.loads(counter_path.read_text())
        calls = data.get("calls", []) if isinstance(data, dict) else []
        now = time.time()
        recent = len([ts for ts in calls if isinstance(ts, int | float) and now - ts <= ROLLING_WINDOW_S])
        exhausted = " - cap exhausted, consults refuse until the window rolls" if recent >= cap else ""
        info(f"consults_last_24h: {recent}/{cap} ({cap_note}){exhausted}")
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"cannot read {counter_path}: {exc}")


def check_state_dir(state_dir: Path, cap: int, *, cap_note: str) -> None:
    if state_dir.exists() and not state_dir.is_dir():
        fail(f"state dir {state_dir} exists but is not a directory - tandem cannot start")
        return
    if not state_dir.is_dir():
        ok(f"state dir {state_dir} not present yet (created on first run)")
        return
    mode = stat.S_IMODE(state_dir.stat().st_mode)
    if mode & 0o077:
        warn(f"state dir {state_dir} is group/other accessible ({oct(mode)}) - expected 0700")
    else:
        ok(f"state dir permissions {oct(mode)}")

    _probe_state_lock(state_dir)
    _consult_count(state_dir, cap, cap_note=cap_note)

    registry = state_dir / "registry.json"
    if registry.is_file():
        try:
            data = json.loads(registry.read_text())
            children = data.get("children", [])
            live = []
            from tandem.state import pid_alive

            for entry in children if isinstance(children, list) else []:
                pgid = entry.get("pgid") if isinstance(entry, dict) else None
                if isinstance(pgid, int) and not isinstance(pgid, bool) and pid_alive(pgid):
                    live.append(entry)
            if live:
                warn(f"{len(live)} registry entries reference live processes (running children or stale rows)")
            else:
                ok("registry clean (no live children)")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            warn(f"cannot parse {registry}: {exc}")
    else:
        ok("no registry yet (no children have run)")

    for name, kind in (("sessions", "session"), ("tasks", "delegation run"), ("runs", "run log")):
        directory = state_dir / name
        if directory.is_dir():
            count = sum(1 for _ in directory.iterdir())
            ok(f"{name}: {count} {kind} files/dirs")
        else:
            ok(f"{name}: none yet")


def main() -> int:
    print("tandem doctor")
    print(f"checkout: {REPO}")
    check_python_and_tandem()

    vibe_bin = os.environ.get("TANDEM_VIBE_BIN", "vibe")
    claude_bin = os.environ.get("TANDEM_CLAUDE_BIN", "claude")
    raw_state_dir = os.environ.get("TANDEM_LOG_DIR", "")
    state_dir = Path(raw_state_dir).expanduser() if raw_state_dir.strip() else Path.home() / ".cache" / "tandem"
    raw_roots = os.environ.get("TANDEM_ALLOWED_ROOTS", "")
    operator_roots = raw_roots if raw_roots.strip() else None

    check_clis(vibe_bin, claude_bin)
    vibe_env = read_vibe_mount(state_dir)
    claude_env = read_claude_mount(state_dir)
    check_mount_roots(vibe_env, claude_env, operator_roots)

    # Consult cap resolution: this shell's knob, else a cap the mounts agree
    # on, else the default. Two mounts disagreeing is itself a warning.
    mount_caps = [
        int(raw)
        for raw in (str(env.get("TANDEM_DAILY_CONSULT_CAP", "")) for env in (vibe_env, claude_env) if env is not None)
        if raw.strip().isdigit()
    ]
    if len(mount_caps) == 2 and mount_caps[0] != mount_caps[1]:
        warn(
            f"vibe and claude mounts set different TANDEM_DAILY_CONSULT_CAP values: {mount_caps[0]} vs {mount_caps[1]}"
        )
    raw_cap = os.environ.get("TANDEM_DAILY_CONSULT_CAP", "")
    if raw_cap.strip().isdigit():
        cap = int(raw_cap)
        cap_note = "cap from this shell"
    elif len(set(mount_caps)) == 1 and mount_caps:
        cap = mount_caps[0]
        cap_note = "cap from mounts"
    else:
        from tandem.config import DEFAULT_DAILY_CONSULT_CAP

        cap = DEFAULT_DAILY_CONSULT_CAP
        cap_note = "default cap"
    check_state_dir(state_dir, cap, cap_note=cap_note)

    knob_names = (
        "TANDEM_ROLE",
        "TANDEM_MAX_CHILDREN",
        "TANDEM_DAILY_CONSULT_CAP",
        "TANDEM_DELEGATE_TRUST",
        "TANDEM_DELEGATE_MAX_PRICE",
        "TANDEM_TASK_TTL_H",
        "TANDEM_KILL_GRACE_S",
        "TANDEM_LOG_CONTENT",
    )
    knobs = {name: os.environ.get(name) for name in knob_names if os.environ.get(name)}
    if knobs:
        print(f"[info]  effective env knobs: {json.dumps(knobs)}")
    else:
        print("[info]  effective env knobs: none set (all defaults)")
    print(f"[info]  mcp versions: {pkg_version('mcp') if _pkg_available('mcp') else 'not installed'}")

    if FAILURES:
        print(f"result: {FAILURES} failure(s), {WARNINGS} warning(s)")
        return 1
    print(f"result: healthy ({WARNINGS} warning(s))")
    return 0


def _pkg_available(name: str) -> bool:
    try:
        pkg_version(name)
    except Exception:
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
