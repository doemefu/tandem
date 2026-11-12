"""Loop guard: tandem tools refused when running inside a tandem-spawned child.

Every CLI child spawned by tandem carries ``TANDEM_CHILD=1`` in its constructed
environment. Since MCP server mounts inherit the host CLI's environment, a
tandem instance mounted by that child observes the same variable and refuses
every tool call: one delegated hop is the maximum depth, so a child can never
consult or delegate on its own. This is the belt to the braces of toolless
Claude children and ``--disabled-tools "re:mcp_.*"`` Vibe children.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from tandem.errors import GuardRefusedError

GUARD_ENV = "TANDEM_CHILD"

GUARD_MESSAGE = (
    "tandem refused: TANDEM_CHILD is set - this is a delegated child, not an "
    "interactive session. Complete your own task and return a result; do not call "
    "tandem tools."
)


def child_mode(env: Mapping[str, str] | None = None) -> bool:
    """Return True when running inside a tandem-spawned child."""
    source = os.environ if env is None else env
    return source.get(GUARD_ENV, "") == "1"


def refusal(tool: str) -> GuardRefusedError:
    """Build the structured loop-guard refusal for ``tool``."""
    return GuardRefusedError(f"tandem tool '{tool}' refused: {GUARD_MESSAGE}")
