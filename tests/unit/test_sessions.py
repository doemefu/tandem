"""Session registry: storage, TTL, id validation, corrupt files."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tandem.sessions import SessionRegistry, valid_session_id
from tandem.state import StatePaths, save_json


def _registry(tmp_path: Path) -> SessionRegistry:
    paths = StatePaths(tmp_path / "state")
    paths.ensure_layout()
    return SessionRegistry(paths)


def test_valid_session_id_accepts_and_rejects() -> None:
    assert valid_session_id("b07e0c55-9f6a")
    assert valid_session_id("A" * 128)
    assert not valid_session_id("short")
    assert not valid_session_id("")
    assert not valid_session_id("../escape")
    assert not valid_session_id("slash/ed")
    assert not valid_session_id("A" * 129)
    assert not valid_session_id("with space")


def test_get_unknown_returns_none(tmp_path: Path) -> None:
    assert _registry(tmp_path).get("b07e0c55-9f6a") is None


def test_upsert_then_get_roundtrip(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    record = registry.upsert("b07e0c55-9f6a", cumulative_cost_usd=0.125, cwd="/srv/project")
    loaded = registry.get("b07e0c55-9f6a")
    assert loaded is not None
    assert loaded.session_id == "b07e0c55-9f6a"
    assert loaded.consults == 1
    assert loaded.consults == record.consults
    assert loaded.cumulative_cost_usd == pytest.approx(0.125)
    assert loaded.created == record.created
    assert loaded.cwd == "/srv/project"  # the resumable cwd pins the resume environment


def test_upsert_refreshes_counts_and_cost(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    first = registry.upsert("b07e0c55-9f6a", cumulative_cost_usd=0.05, cwd="/srv/project")
    second = registry.upsert("b07e0c55-9f6a", cumulative_cost_usd=0.12, cwd="/srv/project")
    assert second.consults == 2
    assert second.created == first.created
    assert second.cwd == "/srv/project"
    assert second.cumulative_cost_usd == pytest.approx(0.12)
    assert registry.get("b07e0c55-9f6a").cumulative_cost_usd == pytest.approx(0.12)  # type: ignore[union-attr]


def test_upsert_rejects_missing_cwd(tmp_path: Path) -> None:
    # A record without a resumable cwd cannot be replayed for a followup.
    with pytest.raises(ValueError, match="cwd"):
        _registry(tmp_path).upsert("b07e0c55-9f6a", cumulative_cost_usd=0.05, cwd="")


def test_record_without_cwd_is_corrupt(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    paths = StatePaths(tmp_path / "state")
    save_json(
        paths.sessions_dir / "b07e0c55-9f6a.json",
        {"created": 0.0, "last_used": 1.0, "consults": 1, "cumulative_cost_usd": 0.05},
    )
    assert registry.get("b07e0c55-9f6a") is None


def test_expired_session_reads_as_unknown(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.upsert("b07e0c55-9f6a", cumulative_cost_usd=0.05, cwd="/srv/project")
    now = time.time()
    assert registry.get("b07e0c55-9f6a", now=now + 25 * 3600) is None


def test_expired_records_restart_on_upsert(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.upsert("b07e0c55-9f6a", cumulative_cost_usd=0.05, cwd="/srv/project", now=time.time() - 25 * 3600)
    fresh = registry.upsert("b07e0c55-9f6a", cumulative_cost_usd=0.1, cwd="/srv/project")
    assert fresh.consults == 1  # the expired record does not count as continuation


def test_corrupt_record_reads_as_unknown(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    paths = StatePaths(tmp_path / "state")
    save_json(paths.sessions_dir / "b07e0c55-9f6a.json", {"garbage": "not a record"})
    assert registry.get("b07e0c55-9f6a") is None


def test_upsert_rejects_invalid_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid session"):
        _registry(tmp_path).upsert("../escape", cumulative_cost_usd=0.0, cwd="/srv/project")


def test_sweep_removes_expired_files(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.upsert("b07e0c55-9f6a", cumulative_cost_usd=None, cwd="/srv/project")
    paths = StatePaths(tmp_path / "state")
    stale = paths.sessions_dir / "aaaaaaaaaaaaaaaa.json"
    save_json(stale, {"old": True})
    os.utime(stale, (time.time() - 25 * 3600, time.time() - 25 * 3600))
    registry.upsert("cccccccccccccccc", cumulative_cost_usd=None, cwd="/srv/project")  # triggers sweep
    assert not stale.exists()
    assert registry.get("cccccccccccccccc") is not None
