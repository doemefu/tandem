"""MCPServer wiring, the shared bridge_status tool and role dispatch.

Role dispatch: ``cfg.role == "thinker"`` registers the consult tools from
``tandem.thinker`` (WP3); ``cfg.role == "worker"`` registers the delegation
tool from ``tandem.worker`` (WP4).  ``bridge_status`` is role-agnostic and
implemented here so the runner, registry, caps and telemetry have a live
MCP host from WP2 on. Real stderr/stdout go to the transport; diagnostics
and unexpected exceptions stay server-side per the SDK contract.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from tandem import __version__
from tandem.child import ChildRegistry, run_child
from tandem.config import Config, build_child_env
from tandem.errors import TandemError
from tandem.guard import child_mode, refusal
from tandem.runtime import Runtime
from tandem.state import RollingCounter

BRIDGE_STATUS_DESCRIPTION = (
    "Report tandem bridge health: the configured role, the Claude Code and Vibe "
    "versions found on PATH, the tandem caps (max parallel children; the rolling-"
    "24h consult budget is tandem's own cap, not the Anthropic quota, which only "
    "surfaces at failure) and the number of currently running children. Read-only."
)

VersionProbe = Callable[[str], Awaitable[str | None]]

_CLI_VERSION_TIMEOUT_S = 5.0
_CLI_VERSION_CAP = 4096


async def probe_cli_version(binary: str, *, timeout_s: float = _CLI_VERSION_TIMEOUT_S) -> str | None:
    """Best-effort one-line ``--version`` probe; None on any failure or empty output."""
    try:
        result = await run_child(
            [binary, "--version"],
            env=build_child_env(),
            timeout_s=timeout_s,
            grace_s=1.0,
            stdout_cap_bytes=_CLI_VERSION_CAP,
            stderr_keep_bytes=2048,
        )
    except TandemError:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            return line.strip()
    return None


async def bridge_status_impl(
    cfg: Config,
    registry: ChildRegistry,
    counter: RollingCounter,
    versions: MutableMapping[str, str | None],
    *,
    probe: VersionProbe = probe_cli_version,
) -> dict[str, Any]:
    """Shared payload builder for the bridge_status tool."""
    if child_mode():
        raise refusal("bridge_status")
    if "claude" not in versions:
        versions["claude"] = await probe(cfg.claude_bin)
        versions["vibe"] = await probe(cfg.vibe_bin)
    calls = await anyio.to_thread.run_sync(counter.count)
    return {
        "role": cfg.role,
        "tandem_version": __version__,
        "claude_version": versions["claude"],
        "vibe_version": versions["vibe"],
        "caps": {
            "max_children": cfg.max_children,
            "daily_consult_cap": cfg.daily_consult_cap,
            "consults_last_24h": calls,
            "daily_cap_remaining": max(0, cfg.daily_consult_cap - calls),
        },
        "running_children": len(registry.own()),
    }


def build_server(runtime: Runtime) -> MCPServer:
    """Create the MCPServer for the runtime's configured role and register its tools."""
    server = MCPServer(name="tandem", version=__version__)
    versions: dict[str, str | None] = {}
    cfg, registry, counter = runtime.cfg, runtime.registry, runtime.counter

    @server.tool(
        name="bridge_status",
        title="Bridge status",
        description=BRIDGE_STATUS_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def bridge_status() -> dict[str, Any]:
        try:
            return await bridge_status_impl(cfg, registry, counter, versions)
        except TandemError as exc:
            raise ToolError(json.dumps(exc.payload(), separators=(",", ":"))) from exc

    if cfg.role == "thinker":
        from tandem import thinker

        thinker.add_tools(server, runtime)
    elif cfg.role == "worker":
        from tandem import worker

        worker.add_tools(server, runtime)

    return server
