#!/bin/sh
# tandem uninstaller: removes the "tandem" mounts from both editors.
#
# The state dir (default ~/.cache/tandem) is left untouched on purpose:
# it holds the run logs and delegated specs. Remove it manually if you
# really want everything gone.
#
# A remove command that fails while the mount is still listed is a hard
# error; "nothing was mounted" is not.

set -eu

VIBE_BIN="${TANDEM_VIBE_BIN:-vibe}"
CLAUDE_BIN="${TANDEM_CLAUDE_BIN:-claude}"

# Matches the server name on its own, not as a fragment of another name.
_name_match='(^|[^[:alnum:]_-])tandem([^[:alnum:]_-]|$)'

# vibe 3.1.1 has no "mcp list" (live-probed: unrecognized subcommand), so a
# failed remove is resolved against the config file itself - both shapes vibe
# writes ([[mcp_servers]] with a name line, and the tolerated keyed-table
# form). claude mcp list exists and stays authoritative on that side.

if command -v "$VIBE_BIN" >/dev/null 2>&1; then
  echo "==> removing Vibe mount"
  if ! "$VIBE_BIN" mcp remove tandem >/dev/null 2>&1; then
    vibe_config="$HOME/.vibe/config.toml"
    if [ -f "$vibe_config" ] &&
      grep -Eq '^[[:space:]]*name[[:space:]]*=[[:space:]]*"tandem"|^\[mcp_servers\.tandem' "$vibe_config"; then
      echo "uninstall.sh: vibe mcp remove failed and the mount is still in $vibe_config" >&2
      exit 1
    fi
    echo "  (nothing to remove)"
  fi
fi

if command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
  echo "==> removing Claude Code mount (user scope)"
  if ! "$CLAUDE_BIN" mcp remove tandem -s user >/dev/null 2>&1; then
    if "$CLAUDE_BIN" mcp list 2>/dev/null | grep -Eq "$_name_match"; then
      echo "uninstall.sh: claude mcp remove failed and the mount is still listed" >&2
      exit 1
    fi
    echo "  (nothing to remove)"
  fi
fi

echo "==> uninstalled. State dir (\${TANDEM_LOG_DIR:-~/.cache/tandem}) kept."
