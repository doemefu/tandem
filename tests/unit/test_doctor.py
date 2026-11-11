"""Doctor script contract: read-only, mount checks, exit semantics."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCTOR = REPO / "scripts" / "doctor.py"


def _run_doctor(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # A doctor run is only meaningful with an isolated HOME: Path.home()
    # may fall back to getpwuid (the real home) when HOME is dropped.
    assert "HOME" in env, "doctor checks must run with an isolated HOME"
    return subprocess.run(
        [sys.executable, str(DOCTOR)],
        capture_output=True,
        text=True,
        env=env
        | {
            "TANDEM_LOG_DIR": env.get("TANDEM_LOG_DIR", "/var/empty/tandem-never"),
            # Keep the machine's PATH so claude/vibe probes are honest
            # unless a test explicitly overrides it.
            "PATH": env.get("PATH", os.environ.get("PATH", "/usr/bin:/bin")),
        },
        timeout=120,
        check=False,
    )


def _tree_listing(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def _write_mounts(
    home: Path,
    *,
    log_dir: str,
    vibe_roots: str,
    claude_roots: str,
    tool_timeout: str = "1860.0",
    startup_timeout: str = "30.0",
) -> None:
    """Both mounts with matching state dirs so only roots can drift.

    The vibe mount uses the real config shape: ``vibe mcp add`` writes an
    array of tables (``[[mcp_servers]]``) keyed by ``name``; the timeout
    fields exist from install.sh's --tool-timeout-sec/--startup-timeout-sec.
    """
    vibe_dir = home / ".vibe"
    vibe_dir.mkdir(parents=True, exist_ok=True)
    toml = (
        "[[mcp_servers]]\n"
        'name = "tandem"\n'
        'transport = "stdio"\n'
        'command = "/x/y/.venv/bin/python"\n'
        f"tool_timeout_sec = {tool_timeout}\n"
        f"startup_timeout_sec = {startup_timeout}\n"
        "[mcp_servers.env]\n"
        'TANDEM_ROLE = "thinker"\n'
        f'TANDEM_LOG_DIR = "{log_dir}"\n'
        f'TANDEM_ALLOWED_ROOTS = "{vibe_roots}"\n'
    )
    (vibe_dir / "config.toml").write_text(toml)
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "tandem": {
                        "command": "/x/y/.venv/bin/python",
                        "env": {
                            "TANDEM_ROLE": "worker",
                            "TANDEM_LOG_DIR": log_dir,
                            "TANDEM_ALLOWED_ROOTS": claude_roots,
                        },
                    }
                }
            }
        )
    )


def test_doctor_reports_missing_mounts_as_warnings(tmp_path: Path) -> None:
    isolated_home = tmp_path / "home"
    isolation = tmp_path / "isolation"
    (isolated_home / ".cache").mkdir(parents=True)
    isolation.mkdir()
    before = _tree_listing(tmp_path)
    result = _run_doctor({"HOME": str(isolated_home), "TMPDIR": str(isolation)})
    assert result.returncode == 0
    assert "[FAIL]" not in result.stdout
    assert "[warn]" in result.stdout
    assert "tandem importable" in result.stdout
    assert "result: healthy" in result.stdout
    # Read-only proof: configs, state and temp dirs stay untouched.
    assert _tree_listing(tmp_path) == before


def test_doctor_missing_clis_are_warnings_not_failures(tmp_path: Path) -> None:
    """Both CLIs absent is a degraded but healthy machine (exit 0)."""
    home = tmp_path / "home"
    empty_bin = tmp_path / "empty-bin"
    home.mkdir()
    empty_bin.mkdir()
    result = _run_doctor({"HOME": str(home), "PATH": str(empty_bin)})
    assert result.returncode == 0
    assert "vibe not usable on PATH" in result.stdout
    assert "claude not usable on PATH" in result.stdout
    assert "result: healthy" in result.stdout


def test_doctor_broken_state_dir_is_a_failure(tmp_path: Path) -> None:
    home = tmp_path / "home"
    broken = tmp_path / "not-a-dir"
    home.mkdir()
    broken.write_text("x")
    result = _run_doctor({"HOME": str(home), "TANDEM_LOG_DIR": str(broken)})
    assert result.returncode == 1
    assert "exists but is not a directory" in result.stdout


def test_doctor_mounted_home_root_is_a_failure(tmp_path: Path) -> None:
    """A mount whose roots grant would make the server refuse to start."""
    home = tmp_path / "home"
    home.mkdir()
    _write_mounts(home, log_dir=str(tmp_path / "state"), vibe_roots=str(home), claude_roots=str(home))
    result = _run_doctor({"HOME": str(home), "TANDEM_LOG_DIR": str(tmp_path / "state")})
    assert result.returncode == 1
    assert "mount roots invalid" in result.stdout
    assert "home directory or an ancestor" in result.stdout


def test_doctor_matching_mounts_produce_no_root_noise(tmp_path: Path) -> None:
    """Installed-and-consistent mounts report info, not phantom warnings."""
    home = tmp_path / "home"
    state = tmp_path / "state"
    (home / "proj").mkdir(parents=True)
    _write_mounts(home, log_dir=str(state), vibe_roots=str(home / "proj"), claude_roots=str(home / "proj"))
    result = _run_doctor({"HOME": str(home), "TANDEM_LOG_DIR": str(state)})
    assert result.returncode == 0
    assert "vibe mount present (role thinker)" in result.stdout
    assert "claude mount present (role worker)" in result.stdout
    assert "grant different roots" not in result.stdout
    assert "differs from the checked state dir" not in result.stdout
    assert "differ from this shell" not in result.stdout
    assert "mounted roots grant" in result.stdout


def test_doctor_mount_root_drift_and_shell_advisory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    (home / "proj").mkdir(parents=True)
    (home / "other").mkdir()
    _write_mounts(home, log_dir=str(state), vibe_roots=str(home / "proj"), claude_roots=str(home / "other"))
    result = _run_doctor({"HOME": str(home), "TANDEM_LOG_DIR": str(state), "TANDEM_ALLOWED_ROOTS": str(home / "proj")})
    assert result.returncode == 0
    assert "grant different roots" in result.stdout
    assert "differ from this shell" in result.stdout


def test_doctor_consult_counter_display(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()
    state.chmod(0o700)
    counter = state / "counters.json"
    now = time.time()
    counter.write_text(json.dumps({"calls": [now - 100, now - 200]}))
    result = _run_doctor({"HOME": str(home), "TANDEM_LOG_DIR": str(state)})
    assert result.returncode == 0
    assert "consults_last_24h: 2/40" in result.stdout
    assert "cap exhausted" not in result.stdout
    # Cap exhausted surfaces as a warning, still exit 0.
    counter.write_text(json.dumps({"calls": [now - 100] * 41}))
    result = _run_doctor({"HOME": str(home), "TANDEM_LOG_DIR": str(state)})
    assert result.returncode == 0
    assert "consults_last_24h: 41/40" in result.stdout
    assert "cap exhausted" in result.stdout


# ---------------------------------------------------------------------------
# Review round 2 (PR #4): mounted host timeouts vs tandem's ceilings
# ---------------------------------------------------------------------------


def test_doctor_healthy_mounted_timeouts(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    (home / "proj").mkdir(parents=True)
    _write_mounts(home, log_dir=str(state), vibe_roots=str(home / "proj"), claude_roots=str(home / "proj"))
    result = _run_doctor({"HOME": str(home), "TANDEM_LOG_DIR": str(state)})
    assert "tool_timeout_sec 1860 covers the 1800s consult ceiling" in result.stdout
    assert "startup_timeout_sec 30" in result.stdout
    assert "under the 1860s" not in result.stdout


def test_doctor_warns_on_low_tool_timeout(tmp_path: Path) -> None:
    """The ~60s host default would cut every long consult - warn, not fail."""
    home = tmp_path / "home"
    state = tmp_path / "state"
    (home / "proj").mkdir(parents=True)
    _write_mounts(
        home,
        log_dir=str(state),
        vibe_roots=str(home / "proj"),
        claude_roots=str(home / "proj"),
        tool_timeout="60.0",
    )
    result = _run_doctor({"HOME": str(home), "TANDEM_LOG_DIR": str(state)})
    assert result.returncode == 0
    assert "tool_timeout_sec 60 sits under the 1860s" in result.stdout
    assert "covers the 1800s consult ceiling" not in result.stdout


def test_doctor_warns_on_missing_timeouts(tmp_path: Path) -> None:
    """A mount predating the timeout flags still gets honest noise."""
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()
    _write_mounts(home, log_dir=str(state), vibe_roots=str(state), claude_roots=str(state))
    # Strip both fields without rewriting the helper: write a bare mount.
    vibe_config = home / ".vibe" / "config.toml"
    toml = (
        vibe_config.read_text().replace("tool_timeout_sec = 1860.0\n", "").replace("startup_timeout_sec = 30.0\n", "")
    )
    vibe_config.write_text(toml)
    result = _run_doctor({"HOME": str(home), "TANDEM_LOG_DIR": str(state)})
    assert result.returncode == 0
    assert "has no tool_timeout_sec" in result.stdout
    assert "has no startup_timeout_sec" in result.stdout
