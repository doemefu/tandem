"""Config parsing, the constructed child environment and the cwd trust boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from tandem.config import Config, build_child_env
from tandem.errors import ConfigError


def _base_env(role: str = "thinker") -> dict[str, str]:
    return {
        "TANDEM_ROLE": role,
        "PATH": "/usr/bin:/bin",
        "HOME": "/Users/test",
        "LANG": "de_DE.UTF-8",
        "LC_ALL": "de_DE.UTF-8",
        "VIBE_SESSION": "1",
        "VIBE_ACTIVE_MODEL": "must-not-leak",
        "TANDEM_LOG_DIR": "/var/tandem-x",
    }


def test_from_env_reads_role() -> None:
    cfg = Config.from_env(_base_env("worker"))
    assert cfg.role == "worker"


def test_from_env_defaults() -> None:
    cfg = Config.from_env(_base_env())
    assert cfg.max_children == 2
    assert cfg.daily_consult_cap == 40
    assert cfg.delegate_trust is True
    assert cfg.content_log is False
    assert cfg.claude_bin == "claude"
    assert cfg.vibe_bin == "vibe"
    assert cfg.state_dir == Path("/var/tandem-x")
    assert cfg.allowed_roots == (Path.cwd(),)


def test_from_env_rejects_missing_role() -> None:
    env = _base_env()
    env.pop("TANDEM_ROLE")
    with pytest.raises(ConfigError, match="TANDEM_ROLE"):
        Config.from_env(env)


def test_from_env_rejects_unknown_role() -> None:
    with pytest.raises(ConfigError, match="TANDEM_ROLE"):
        Config.from_env(_base_env("supervisor"))


def test_from_env_rejects_bad_knob() -> None:
    with pytest.raises(ConfigError, match="TANDEM_MAX_CHILDREN"):
        Config.from_env(_base_env() | {"TANDEM_MAX_CHILDREN": "two"})


def test_from_env_rejects_below_minimum() -> None:
    with pytest.raises(ConfigError):
        Config.from_env(_base_env() | {"TANDEM_MAX_CHILDREN": "0"})


def test_build_child_env_filters_and_marks() -> None:
    base = _base_env() | {
        "SECRET": "s3cr3t",
        "TMPDIR": "/var/empty",
        "TZ": "Europe/Zurich",
        "SSL_CERT_FILE": "/etc/ca.pem",
        "TANDEM_MAX_CHILDREN": "5",
    }
    env = build_child_env(base)
    assert env["TANDEM_CHILD"] == "1"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/Users/test"
    assert env["LC_ALL"] == "de_DE.UTF-8"
    assert env["TZ"] == "Europe/Zurich"
    assert env["SSL_CERT_FILE"] == "/etc/ca.pem"
    assert env["TANDEM_LOG_DIR"] == "/var/tandem-x"
    assert env["TANDEM_MAX_CHILDREN"] == "5"
    # Never forwarded: the role (nested mounts pick their own), session markers,
    # VIBE_* config overrides, and anything outside the allowlist.
    assert "TANDEM_ROLE" not in env
    assert "VIBE_SESSION" not in env
    assert "VIBE_ACTIVE_MODEL" not in env
    assert "SECRET" not in env


def test_is_cwd_allowed_boundary(tmp_path: Path) -> None:
    inner = tmp_path / "proj"
    inner.mkdir()
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir()
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    cfg = Config.from_env(_base_env() | {"TANDEM_ALLOWED_ROOTS": str(tmp_path)})
    assert cfg.is_cwd_allowed(inner)
    assert not cfg.is_cwd_allowed(outside)
    assert not cfg.is_cwd_allowed(a_file)
    assert not cfg.is_cwd_allowed(tmp_path / "missing")
    assert not cfg.is_cwd_allowed(Path.home())
    assert not cfg.is_cwd_allowed(Path("/"))
