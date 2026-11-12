"""Vibe adapter: delegate argv and export.json mapping.

The export.json schema is only partially pinned: ``stop_reason`` values
are verified against vibe 3.1.1 help text and research (finished,
turn_limit, token_limit, price_limit, deadline, terminated, usage_error,
config_error, infrastructure_failure, refusal, aborted, length), and the
committed fixtures are synthesized until the WP6 window replaces them
with live captures.  Everything else is parsed defensively: optional
fields are harvested by candidate key names, and a missing or unreadable
export degrades to type ``child_crash`` rather than crashing the tool.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tandem.config import Config

STOP_REASON_LIMITS = frozenset(("turn_limit", "token_limit", "price_limit", "deadline", "length"))
STOP_REASON_ERRORS = frozenset(("usage_error", "config_error", "infrastructure_failure", "refusal", "aborted"))

DELEGATE_DISABLED_TOOLS = "re:mcp_.*"
DELEGATE_DISABLED_TOOLS_CONNECTORS = "re:connector_.*"
EXPORT_READ_CAP_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class VibeOutcome:
    """Fields tandem uses from one delegation run (export.json + exit)."""

    stop_reason: str | None
    session_id: str | None
    answer: str | None
    cost_usd: float | None
    warnings: tuple[str, ...]
    raw: dict[str, Any] | None


def build_delegate_args(
    cfg: Config,
    *,
    workdir: Path,
    prompt_file: Path,
    output_dir: Path,
    max_seconds: float,
    agent: str,
    max_price_usd: float | None = None,
) -> list[str]:
    """Argv for one headless vibe delegation.

    ``--prompt-file`` alone enters programmatic mode (no -p flag); the
    prompt never touches argv.  Two repeatable ``--disabled-tools``
    regexes keep the child from reaching other MCP servers (loop
    prevention) or any built-in connector (network tightness for a
    delegated run); ``--trust`` grants the workdir reputation within
    this invocation only when the operator enabled delegate_trust.
    ``--max-price`` is passed only when the operator set a per-run cap.
    """
    argv = [
        cfg.vibe_bin,
        "--workdir",
        str(workdir),
        "--prompt-file",
        str(prompt_file),
        "--output-dir",
        str(output_dir),
        "--time-limit",
        str(int(max_seconds)),
        "--agent",
        agent,
        "--disabled-tools",
        DELEGATE_DISABLED_TOOLS,
        "--disabled-tools",
        DELEGATE_DISABLED_TOOLS_CONNECTORS,
    ]
    if max_price_usd is not None:
        argv += ["--max-price", str(max_price_usd)]
    if cfg.delegate_trust:
        argv += ["--trust"]
    return argv


def _first_str(raw: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str):
            return value
    return None


def _first_float(raw: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, bool):
            continue
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            continue
    return None


def _warnings(raw: dict[str, Any]) -> tuple[str, ...]:
    """Warnings as caller-readable strings.

    Structured entries (the live export carries nested dicts) serialize as
    JSON - a Python ``repr`` of a dict is not a contract the caller wants to
    parse.
    """
    candidates: list[Any] = []
    for key in ("warnings", "permission_denials"):
        value = raw.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    out: list[str] = []
    for item in candidates[:50]:
        if isinstance(item, (dict, list)):
            out.append(json.dumps(item, default=str))
        else:
            out.append(str(item))
    return tuple(out)


def _answer(raw: dict[str, Any]) -> str | None:
    """Harvest the report from the export (stdout may have died mid-write).

    The live export schema nests the answer under ``outcome``; the top-level
    ``answer`` key is defensive-only (no live vibe export ever carried it) -
    kept so a schema regression degrades instead of losing the one field
    that rescues an empty stdout.
    """
    for source in (raw.get("outcome"), raw):
        if isinstance(source, dict):
            value = source.get("answer")
            if isinstance(value, str):
                return value
    return None


def parse_export(export_path: Path) -> VibeOutcome | None:
    """Parse export.json defensively; None when missing, unreadable or corrupt.

    The read is capped: a runaway export (or an attacker-touched file in the
    state dir) cannot balloon the server's memory.
    """
    try:
        with export_path.open("rb") as handle:
            raw_bytes = handle.read(EXPORT_READ_CAP_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        raw = json.loads(raw_bytes[:EXPORT_READ_CAP_BYTES])
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return VibeOutcome(
        stop_reason=_first_str(raw, ("stop_reason",)),
        session_id=_first_str(raw, ("session_id",)),
        answer=_answer(raw),
        cost_usd=_first_float(raw, ("cost_usd", "total_cost_usd")),
        warnings=_warnings(raw),
        raw=raw,
    )
