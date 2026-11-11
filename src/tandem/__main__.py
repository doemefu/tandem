"""Entrypoint of the tandem stdio MCP server.

The tool set is selected by the ``TANDEM_ROLE`` environment variable: "thinker"
(mounted in Vibe, consults Claude) or "worker" (mounted in Claude Code, delegates
to Vibe). A missing or unknown role is a configuration error: the server refuses
to start (exit 2) instead of guessing. Protocol traffic never goes to stdout, so
diagnostics are written to stderr.
"""

import os
import sys

from tandem import ROLES, __version__

EXIT_CONFIG_ERROR = 2


def main() -> int:
    role = os.environ.get("TANDEM_ROLE", "")
    if role not in ROLES:
        print(
            f"tandem {__version__}: refusing to start: TANDEM_ROLE must be one of {', '.join(ROLES)}, got {role!r}",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR
    print(
        f"tandem {__version__}: role '{role}' validated (bootstrap: MCP server wiring not implemented yet)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
