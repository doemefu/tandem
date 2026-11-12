#!/bin/sh
# tandem installer.
#
# Mounts the tandem MCP server in both editors:
#   - Vibe (user config):          role "thinker"  -> consult_claude tools
#   - Claude Code (user scope):    role "worker"   -> delegate_to_vibe tool
#
# Usage:
#   scripts/install.sh --roots /path/to/projects:/path/to/other --state-dir DIR
#
#   --roots         Directories tandem may operate in (colon-separated,
#                   whitespace inside entries preserved). Exactly the
#                   server rule (tandem.config._reject_home_roots): the
#                   home directory itself, or any ancestor of it (e.g. /),
#                   is refused; directories under home are fine spaces
#                   included.
#   --state-dir     tandem state dir (default: ~/.cache/tandem; an ambient
#                   TANDEM_LOG_DIR in the invoking shell overrides it).
#   --vibe-bin      Override the vibe binary (resolved to its absolute
#   --claude-bin    path and pinned into the mounts if found on PATH).
#   --no-vibe       skip the Vibe mount
#   --no-claude     skip the Claude Code mount
#
# Idempotent: existing "tandem" mounts are removed first. Validation
# happens entirely before uv sync and before any mount write. Paths
# containing quotes or backslashes are refused: they cannot be embedded
# in the claude mount JSON safely.

set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VENV_PY="$REPO/.venv/bin/python"
STATE_DIR="${TANDEM_LOG_DIR:-$HOME/.cache/tandem}"
ROOTS=""
VIBE_BIN="${TANDEM_VIBE_BIN:-vibe}"
CLAUDE_BIN="${TANDEM_CLAUDE_BIN:-claude}"
MOUNT_VIBE=1
MOUNT_CLAUDE=1
# The vibe host kills slow MCP tools at its own tool timeout (60s by default),
# which would cut every consult above a minute.  A consult may legitimately
# run 1800s (consult_claude max_seconds); 1860 adds settle/headroom for the
# result tail and matches the doctor's mounted-timeout check.  Server start
# (fork + import + config validation) is quick; 30s covers a cold venv python
# start on a busy machine without hiding a hung mount.
TOOL_TIMEOUT_SEC=1860
STARTUP_TIMEOUT_SEC=30

while [ "$#" -gt 0 ]; do
  case "$1" in
    --roots)
      if [ "$#" -lt 2 ]; then echo "install.sh: --roots expects a value" >&2; exit 2; fi
      ROOTS="$2"; shift 2 ;;
    --state-dir)
      if [ "$#" -lt 2 ]; then echo "install.sh: --state-dir expects a value" >&2; exit 2; fi
      STATE_DIR="$2"; shift 2 ;;
    --vibe-bin)
      if [ "$#" -lt 2 ]; then echo "install.sh: --vibe-bin expects a value" >&2; exit 2; fi
      VIBE_BIN="$2"; shift 2 ;;
    --claude-bin)
      if [ "$#" -lt 2 ]; then echo "install.sh: --claude-bin expects a value" >&2; exit 2; fi
      CLAUDE_BIN="$2"; shift 2 ;;
    --no-vibe) MOUNT_VIBE=0; shift ;;
    --no-claude) MOUNT_CLAUDE=0; shift ;;
    -h|--help) awk 'NR>1 {if ($0 !~ /^#/) exit; print}' "$0"; exit 0 ;;
    *) echo "install.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$ROOTS" ]; then
  echo "install.sh: --roots is required (colon-separated directories tandem may operate in)." >&2
  echo "  e.g. scripts/install.sh --roots \$HOME/informatik" >&2
  exit 2
fi

# The server compares resolved (physical) paths against the resolved home;
# mirror that here so validation and runtime agree on the same rule.
R_HOME=$(CDPATH= cd -- "$HOME" && pwd -P) || { echo "install.sh: cannot resolve \$HOME" >&2; exit 2; }

# Split on the PATH-like delimiter only; IFS holds no whitespace, so
# spaces inside roots survive. Globbing stays off while splitting.
oldifs=$IFS
IFS=:
set -f
set -- $ROOTS
set +f
IFS=$oldifs
if [ "$#" -eq 0 ]; then
  echo "install.sh: --roots has no entries" >&2
  exit 2
fi

for root do
  [ -n "$root" ] || continue
  if [ ! -d "$root" ]; then
    echo "install.sh: root $root is not a directory" >&2
    exit 2
  fi
  resolved=$(CDPATH= cd -- "$root" && pwd -P) || { echo "install.sh: cannot resolve root $root" >&2; exit 2; }
  # Refuse when home is at or below the root (root == home, or an ancestor).
  case "$R_HOME" in
    "$resolved"|"$resolved"/*)
      echo "install.sh: root $resolved is the home directory or an ancestor of it - allowlist projects, not the home tree" >&2
      exit 2 ;;
  esac
done

# Binary overrides are pinned to absolute paths when resolvable, so mounts
# keep working when the MCP host starts with a different PATH (GUI launch).
if command -v -- "$VIBE_BIN" >/dev/null 2>&1; then
  VIBE_BIN=$(command -v -- "$VIBE_BIN")
fi
if command -v -- "$CLAUDE_BIN" >/dev/null 2>&1; then
  CLAUDE_BIN=$(command -v -- "$CLAUDE_BIN")
fi

case "$STATE_DIR $ROOTS $REPO $VENV_PY $VIBE_BIN $CLAUDE_BIN" in
  *'"'*|*'\'*)
    echo "install.sh: refusing paths containing quotes or backslashes (they cannot be embedded in the claude mount JSON)" >&2
    exit 2 ;;
esac

command -v uv >/dev/null 2>&1 || { echo "install.sh: uv not found on PATH" >&2; exit 1; }
echo "==> uv sync (creates/refreshes $REPO/.venv)"
cd "$REPO"
uv sync --locked
[ -x "$VENV_PY" ] || { echo "install.sh: $VENV_PY missing after uv sync" >&2; exit 1; }

[ -d "$REPO/src/tandem" ] || { echo "install.sh: $REPO/src/tandem missing - run from a tandem checkout" >&2; exit 1; }

if [ "$MOUNT_VIBE" = 1 ]; then
  command -v -- "$VIBE_BIN" >/dev/null 2>&1 || { echo "install.sh: $VIBE_BIN not found on PATH (skip with --no-vibe)" >&2; exit 1; }
  echo "==> mounting thinker role in Vibe (user config)"
  "$VIBE_BIN" mcp remove tandem >/dev/null 2>&1 || true
  # --arg values use the = form: a bare "-m" reads as a flag on vibe 3.1.1
  # (live-verified: "unexpected argument '-m' found" without it).
  # shellcheck disable=SC2086
  "$VIBE_BIN" mcp add tandem --transport stdio \
    --command "$VENV_PY" \
    --arg=-m \
    --arg=tandem \
    --tool-timeout-sec "$TOOL_TIMEOUT_SEC" \
    --startup-timeout-sec "$STARTUP_TIMEOUT_SEC" \
    --env "TANDEM_ROLE=thinker" \
    --env "PYTHONPATH=$REPO/src" \
    --env "TANDEM_LOG_DIR=$STATE_DIR" \
    --env "TANDEM_ALLOWED_ROOTS=$ROOTS" \
    --env "TANDEM_VIBE_BIN=$VIBE_BIN" \
    --env "TANDEM_CLAUDE_BIN=$CLAUDE_BIN"
fi

if [ "$MOUNT_CLAUDE" = 1 ]; then
  command -v -- "$CLAUDE_BIN" >/dev/null 2>&1 || { echo "install.sh: $CLAUDE_BIN not found on PATH (skip with --no-claude)" >&2; exit 1; }
  echo "==> mounting worker role in Claude Code (user scope)"
  "$CLAUDE_BIN" mcp remove tandem -s user >/dev/null 2>&1 || true
  "$CLAUDE_BIN" mcp add-json tandem -s user "$(printf '{
    "command": "%s",
    "args": ["-m", "tandem"],
    "env": {
      "TANDEM_ROLE": "worker",
      "PYTHONPATH": "%s",
      "TANDEM_LOG_DIR": "%s",
      "TANDEM_ALLOWED_ROOTS": "%s",
      "TANDEM_VIBE_BIN": "%s",
      "TANDEM_CLAUDE_BIN": "%s"
    }
  }' "$VENV_PY" "$REPO/src" "$STATE_DIR" "$ROOTS" "$VIBE_BIN" "$CLAUDE_BIN")"
fi

echo "==> installed. State dir (as mounted; '~' expands at server start): $STATE_DIR"
echo "    Verify with: uv run python scripts/doctor.py"
