"""bridge_status payload, version caching, guard refusal and role dispatch."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tandem.child import ChildRegistry, RegistryEntry
from tandem.config import Config
from tandem.errors import GuardRefusedError
from tandem.runlog import RunLog
from tandem.runtime import Runtime
from tandem.server import bridge_status_impl, build_server
from tandem.sessions import SessionRegistry
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


def _runtime(tmp_path: Path, role: str) -> Runtime:
    cfg = _cfg(tmp_path / "state", role)
    paths = StatePaths(tmp_path / "state")
    paths.ensure_layout()
    return Runtime(
        cfg=cfg,
        paths=paths,
        registry=ChildRegistry(paths),
        counter=RollingCounter(paths.counters_file),
        sessions=SessionRegistry(paths),
        log=RunLog(paths.runs_dir, role=role),
    )


def test_build_server_registers_role_tools(tmp_path: Path) -> None:
    thinker_server = build_server(_runtime(tmp_path / "thinker", "thinker"))
    thinker_names = {tool.name for tool in asyncio.run(thinker_server.list_tools())}
    assert thinker_names == {"bridge_status", "consult_claude", "consult_claude_followup"}

    worker_dir = tmp_path / "worker-home"
    worker_server = build_server(_runtime(worker_dir, "worker"))
    worker_names = {tool.name for tool in asyncio.run(worker_server.list_tools())}
    assert worker_names == {"bridge_status", "delegate_to_vibe"}


def test_tool_error_payload_reaches_caller(tmp_path: Path) -> None:
    server = build_server(_runtime(tmp_path, "thinker"))
    with pytest.raises(ToolError) as excinfo:
        asyncio.run(server.call_tool("consult_claude", {"question": "q", "mode": "beg"}))
    raw = str(excinfo.value)
    prefix = "Error executing tool consult_claude: "
    assert raw.startswith(prefix)
    payload = json.loads(raw[len(prefix) :])
    assert payload["is_error"] is True
    assert payload["error_kind"] == "invalid_arguments"


def test_ensure_cli_versions_probes_once_and_fills_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The runtime cache starts empty; the probe path is injected so no real
    # CLI runs.  Both keys must appear together: a concurrent bridge_status
    # in the probe window must not see a half-filled cache (missing "vibe").
    import tandem.server as server_module

    calls: list[str] = []

    async def probe(binary: str) -> str | None:
        calls.append(binary)
        return f"fake-{binary}"

    monkeypatch.setattr(server_module, "probe_cli_version", probe)
    rt = _runtime(tmp_path, "thinker")
    assert rt.versions == {}
    asyncio.run(server_module.ensure_cli_versions(rt))
    assert calls == [rt.cfg.claude_bin, rt.cfg.vibe_bin]
    assert rt.versions == {"claude": f"fake-{rt.cfg.claude_bin}", "vibe": f"fake-{rt.cfg.vibe_bin}"}
    # Cached from here on: no second probe.
    asyncio.run(server_module.ensure_cli_versions(rt))
    assert len(calls) == 2
