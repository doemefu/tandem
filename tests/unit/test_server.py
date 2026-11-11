"""bridge_status payload, version caching and guard refusal (impl level)."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from tandem.child import ChildRegistry, RegistryEntry
from tandem.config import Config
from tandem.errors import GuardRefusedError
from tandem.server import bridge_status_impl
from tandem.state import RollingCounter, StatePaths


async def _probe_fake(binary: str) -> str | None:
    return f"fake-{binary}"


def _cfg(state_dir: Path, role: str = "thinker") -> Config:
    return Config.from_env(
        {
            "TANDEM_ROLE": role,
            "TANDEM_LOG_DIR": str(state_dir),
            "TANDEM_MAX_CHILDREN": "3",
            "TANDEM_DAILY_CONSULT_CAP": "7",
        }
    )


def _setup(tmp_path: Path, role: str = "thinker") -> tuple[Config, ChildRegistry, RollingCounter]:
    paths = StatePaths(tmp_path / "state")
    paths.ensure_layout()
    return _cfg(tmp_path / "state", role), ChildRegistry(paths), RollingCounter(paths.counters_file)


def test_bridge_status_payload(tmp_path: Path) -> None:
    cfg, registry, counter = _setup(tmp_path)
    payload = asyncio.run(bridge_status_impl(cfg, registry, counter, {}, probe=_probe_fake))
    assert payload["role"] == "thinker"
    assert payload["claude_version"] == "fake-claude"
    assert payload["vibe_version"] == "fake-vibe"
    assert payload["tandem_version"]
    caps = payload["caps"]
    assert caps["max_children"] == 3
    assert caps["daily_consult_cap"] == 7
    assert caps["consults_last_24h"] == 0
    assert caps["daily_cap_remaining"] == 7
    assert payload["running_children"] == 0


def test_bridge_status_probes_versions_once(tmp_path: Path) -> None:
    cfg, registry, counter = _setup(tmp_path, role="worker")
    calls: list[str] = []

    async def probe(binary: str) -> str | None:
        calls.append(binary)
        return "v"

    versions: dict[str, str | None] = {}
    asyncio.run(bridge_status_impl(cfg, registry, counter, versions, probe=probe))
    asyncio.run(bridge_status_impl(cfg, registry, counter, versions, probe=probe))
    assert calls == ["claude", "vibe"]


def test_bridge_status_counts_consults(tmp_path: Path) -> None:
    cfg, registry, counter = _setup(tmp_path)
    counter.add()
    counter.add()
    payload = asyncio.run(bridge_status_impl(cfg, registry, counter, {}, probe=_probe_fake))
    assert payload["caps"]["consults_last_24h"] == 2
    assert payload["caps"]["daily_cap_remaining"] == 5


def test_bridge_status_running_children(tmp_path: Path) -> None:
    cfg, registry, counter = _setup(tmp_path)
    registry.register(
        RegistryEntry(
            run_id="r",
            pgid=os.getpid(),
            started_at=time.time(),
            argv_hash="h",
            owner_pid=os.getpid(),
        )
    )
    payload = asyncio.run(bridge_status_impl(cfg, registry, counter, {}, probe=_probe_fake))
    assert payload["running_children"] == 1


def test_bridge_status_refused_in_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, registry, counter = _setup(tmp_path)
    monkeypatch.setenv("TANDEM_CHILD", "1")
    with pytest.raises(GuardRefusedError) as excinfo:
        asyncio.run(bridge_status_impl(cfg, registry, counter, {}, probe=_probe_fake))
    assert excinfo.value.error_kind == "loop_guard"
