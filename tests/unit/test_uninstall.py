"""Uninstaller failure-detection contract with fake CLIs.

vibe 3.1.1 has no `mcp list` (the guard that originally used it was dead
code), so a failed `vibe mcp remove` is resolved against the vibe config
file itself: a tandem entry still present after a failed remove is a hard
error; a machine with no mount reports `(nothing to remove)` and exits 0.
Both tests run against fake CLIs under an isolated HOME - no editor config
of the real user is touched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UNINSTALL = REPO / "scripts" / "uninstall.sh"


def _run_uninstall(home: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(UNINSTALL)],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"},
        timeout=30,
        check=False,
    )


def _fake_bins(bin_dir: Path) -> None:
    """Both CLIs present; both `mcp remove` calls fail (the worst case)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("vibe", "claude"):
        script = bin_dir / name
        script.write_text("#!/bin/sh\nexit 1\n")
        script.chmod(0o755)


def test_uninstall_failed_remove_with_mount_is_hard_error(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".vibe").mkdir()
    (home / ".vibe" / "config.toml").write_text(
        '[[mcp_servers]]\nname = "tandem"\ntransport = "stdio"\n'
        'command = "/x/y/.venv/bin/python"\n[mcp_servers.env]\nTANDEM_ROLE = "thinker"\n'
    )
    bin_dir = tmp_path / "bin"
    _fake_bins(bin_dir)
    result = _run_uninstall(home, bin_dir)
    assert result.returncode == 1
    assert "still in" in result.stderr
    assert "config.toml" in result.stderr


def test_uninstall_keyed_table_shape_also_detected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".vibe").mkdir()
    (home / ".vibe" / "config.toml").write_text('[mcp_servers.tandem]\ntransport = "stdio"\n')
    bin_dir = tmp_path / "bin"
    _fake_bins(bin_dir)
    result = _run_uninstall(home, bin_dir)
    assert result.returncode == 1


def test_uninstall_nothing_mounted_is_clean(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    _fake_bins(bin_dir)
    result = _run_uninstall(home, bin_dir)
    assert result.returncode == 0
    assert "(nothing to remove)" in result.stdout
