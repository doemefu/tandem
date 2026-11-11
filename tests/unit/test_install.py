"""Installer argument-validation contract plus one fake-CLI mount test.

Every rejection case aborts inside argument/root validation, which happens
before ``uv sync`` and before any mount write - so those tests never touch
the user's editor configs. ``test_install_mount_pins_host_timeouts`` is the
exception: it runs the full flow against fake CLIs under an isolated HOME
and asserts the argv only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "scripts" / "install.sh"


def _run_install(isolated_home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(INSTALL), *args],
        capture_output=True,
        text=True,
        env={"HOME": str(isolated_home), "PATH": "/usr/bin:/bin"},
        timeout=30,
        check=False,
    )


def _iso_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    return home


def _assert_nothing_written(home: Path, result: subprocess.CompletedProcess[str]) -> None:
    assert not (home / ".vibe").exists(), result.stderr
    assert not (home / ".claude.json").exists(), result.stderr


def test_install_refuses_home_as_root(tmp_path: Path) -> None:
    home = _iso_home(tmp_path)
    result = _run_install(home, "--roots", str(home))
    assert result.returncode == 2
    assert "home directory or an ancestor" in result.stderr
    _assert_nothing_written(home, result)


def test_install_refuses_ancestor_of_home(tmp_path: Path) -> None:
    home = _iso_home(tmp_path)
    result = _run_install(home, "--roots", str(tmp_path))
    assert result.returncode == 2
    assert "home directory or an ancestor" in result.stderr
    _assert_nothing_written(home, result)


def test_install_accepts_directories_under_home_once_validated(tmp_path: Path) -> None:
    """Regression: home descendants must pass validation (dies at the uv
    check with no uv on PATH, not at root validation)."""
    home = _iso_home(tmp_path)
    result = _run_install(home, "--roots", str(home / "proj"))
    assert result.returncode == 1
    assert "uv not found on PATH" in result.stderr
    assert "home" not in result.stderr
    _assert_nothing_written(home, result)


def test_install_preserves_whitespace_in_roots(tmp_path: Path) -> None:
    """Regression: only ':' delimits; a 'dir with space' must be validated
    whole (the run must reach the home check for the second entry, not die
    on space-fragments claimed missing)."""
    home = _iso_home(tmp_path)
    with_space = home / "dir with space"
    with_space.mkdir()
    result = _run_install(home, "--roots", f"{with_space}:{home}")
    assert result.returncode == 2
    assert "home directory or an ancestor" in result.stderr
    assert "is not a directory" not in result.stderr
    _assert_nothing_written(home, result)


def test_install_rejects_missing_directory(tmp_path: Path) -> None:
    home = _iso_home(tmp_path)
    result = _run_install(home, "--roots", str(home / "missing"))
    assert result.returncode == 2
    assert "is not a directory" in result.stderr


def test_install_rejects_missing_option_value_from_shell_run_of_script(tmp_path: Path) -> None:
    home = _iso_home(tmp_path)
    result = _run_install(home, "--roots")
    assert result.returncode == 2
    assert "--roots expects a value" in result.stderr
    assert "unbound" not in result.stderr


def test_install_rejects_quotes_in_paths(tmp_path: Path) -> None:
    """Fail closed instead of producing broken claude-mount JSON."""
    home = _iso_home(tmp_path)
    result = _run_install(home, "--roots", str(home / "proj"), "--state-dir", 'a"b')
    assert result.returncode == 2
    assert "quotes or backslashes" in result.stderr
    _assert_nothing_written(home, result)


def test_install_rejects_unknown_argument(tmp_path: Path) -> None:
    home = _iso_home(tmp_path)
    result = _run_install(home, "--bogus")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


def test_install_help_prints_the_comment_header_only(tmp_path: Path) -> None:
    home = _iso_home(tmp_path)
    result = _run_install(home, "--help")
    assert result.returncode == 0
    assert result.stdout.startswith("# tandem installer.")
    for line in result.stdout.splitlines():
        assert line.startswith("#")


def test_install_mount_pins_host_timeouts(tmp_path: Path) -> None:
    """The vibe host's ~60s tool timeout would cut long consults; the mount
    must carry --tool-timeout-sec 1860 (1800s consult ceiling + headroom) and
    --startup-timeout-sec 30.  Full run with fake CLIs so both mounts execute."""
    home = _iso_home(tmp_path)
    fake_bin = tmp_path / "bin"
    log = tmp_path / "argv.log"
    fake_bin.mkdir()
    for name in ("uv", "vibe", "claude"):
        script = fake_bin / name
        script.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 0\n')
        script.chmod(0o755)
    # The fake uv skips sync; the repo's existing venv python satisfies the check.
    result = subprocess.run(
        ["/bin/sh", str(INSTALL), "--roots", str(home / "proj"), "--state-dir", str(tmp_path / "state")],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"},
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    vibe_add = next(line for line in calls if line.startswith("vibe mcp add tandem"))
    assert "--tool-timeout-sec 1860" in vibe_add
    assert "--startup-timeout-sec 30" in vibe_add
