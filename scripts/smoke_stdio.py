"""One-shot stdio smoke test for the tandem server.

Starts the server exactly the way an MCP host does (subprocess with
TANDEM_ROLE + PYTHONPATH env), performs the initial handshake, lists the
tools exposed by the role, calls bridge_status and prints the JSON payload.

Usage:
    uv run python scripts/smoke_stdio.py            # default role: thinker
    SMOKE_ROLE=worker uv run python scripts/smoke_stdio.py

Exit code 0 means: handshake ok, exactly the expected tool set registered,
bridge_status answered without error. Any deviation exits non-zero.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SRC = str(Path(__file__).resolve().parents[1] / "src")
EXPECTED_TOOLS = {
    "thinker": {"bridge_status", "consult_claude", "consult_claude_followup"},
    "worker": {"bridge_status", "delegate_to_vibe"},
}


async def smoke(role: str) -> int:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/var/empty"),
        "TANDEM_ROLE": role,
        "PYTHONPATH": SRC,
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "tandem"], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        print(f"protocol={init.protocol_version} server={init.server_info.name} {init.server_info.version}")
        listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
        print(f"tools[{role}]={sorted(names)}")
        expected = EXPECTED_TOOLS[role]
        if names != expected:
            print(f"FAIL: expected {sorted(expected)}, got {sorted(names)}")
            return 1
        result = await session.call_tool("bridge_status", {})
        if result.is_error:
            print("FAIL: bridge_status returned isError")
            for block in result.content:
                if getattr(block, "text", None):
                    print(block.text)
            return 1
        result2 = await session.call_tool("bridge_status", {})
        for block in result2.content:
            text = getattr(block, "text", None)
            if text:
                print(json.dumps(json.loads(text), indent=2, sort_keys=True))
    return 0


def main() -> int:
    role = os.environ.get("SMOKE_ROLE", "thinker")
    if role not in EXPECTED_TOOLS:
        print(f"unknown role {role!r}", file=sys.stderr)
        return 2
    return anyio.run(smoke, role)


if __name__ == "__main__":
    raise SystemExit(main())
