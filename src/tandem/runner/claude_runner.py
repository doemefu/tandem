"""Claude Code adapter: consult argv and tolerant ``-p --output-format json`` parsing.

The JSON shape is pinned by committed fixtures (a live capture on the
quota wall, plus a synthesized success fixture).  Parsing never raises:
Claude writes human-oriented lines to stdout sometimes (update notices,
warnings), so the parser first tries whole-stdout JSON, then scans lines
in reverse for the last object with ``"type": "result"``.  Files here
are import-time constant (no blocking pathlib at call time in async).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from tandem.child import ChildResult
from tandem.config import Config

# Hard isolation of the consult child: no built-in tools, no MCP mounts, no
# settings/hook/CLAUDE.md discovery (verified on claude 2.1.321 via --help;
# auth still comes from the operator's on-disk credentials or env vars).
ZERO_TOOL_ARGS = (
    "--tools",
    "",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--setting-sources",
    "",
)


def build_consult_args(cfg: Config, *, session_id: str | None, resume_id: str | None) -> list[str]:
    """Argv for one consult; exactly one of session_id / resume_id must be set."""
    argv = [cfg.claude_bin, "-p", "--output-format", "json", *ZERO_TOOL_ARGS]
    if session_id is not None and resume_id is None:
        argv += ["--session-id", session_id]
    elif resume_id is not None and session_id is None:
        argv += ["--resume", resume_id]
    else:
        msg = "exactly one of session_id / resume_id must be provided"
        raise ValueError(msg)
    return argv


@dataclass(frozen=True)
class ClaudeOutcome:
    """Fields tandem uses from a parsed Claude result object."""

    answer: str | None
    session_id: str | None
    num_turns: int | None
    cost_usd: float | None
    is_error: bool
    api_error: str | None
    api_error_status: int | None
    subtype: str | None


def _extract_outcome(parsed: Any) -> ClaudeOutcome:
    def opt_str(key: str) -> str | None:
        value = parsed.get(key)
        return value if isinstance(value, str) else None

    def opt_int(key: str) -> int | None:
        value = parsed.get(key)
        if isinstance(value, bool):  # bool is an int subclass; reject it
            return None
        return value if isinstance(value, int) else None

    def opt_float(key: str) -> float | None:
        value = parsed.get(key)
        if isinstance(value, bool):
            return None
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    return ClaudeOutcome(
        answer=opt_str("result"),
        session_id=opt_str("session_id"),
        num_turns=opt_int("num_turns"),
        cost_usd=opt_float("total_cost_usd"),
        is_error=bool(parsed.get("is_error")),
        api_error=opt_str("api_error"),
        api_error_status=opt_int("api_error_status"),
        subtype=opt_str("subtype"),
    )


def _scan_lines(stdout: str) -> Any | None:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "result":
            return parsed
    return None


def parse_claude_result(child: ChildResult) -> ClaudeOutcome | None:
    """Best-effort parse of a Claude child result; None when no result object is found."""
    stdout = child.stdout.decode("utf-8", errors="replace").strip()
    if not stdout:
        return None
    parsed: Any = None
    try:
        whole = json.loads(stdout)
        parsed = whole if isinstance(whole, dict) else None
    except json.JSONDecodeError:
        parsed = None
    if parsed is None:
        parsed = _scan_lines(stdout)
    if not isinstance(parsed, dict) or parsed.get("type") != "result":
        return None
    return _extract_outcome(parsed)
