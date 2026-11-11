# tandem

An MCP bridge that lets **Claude Code** and **Mistral Vibe** work together on one
machine without looping or fighting: **Claude Code does the thinking, Vibe does
the implementing.**

```
Claude Code (decisions)                             Vibe (implementation)
   |                                                   |
   |  worker role MCP mount                            |  thinker role MCP mount
   v                                                   v
   +--------------------> tandem <--------------------+
   |  delegate_to_vibe        consult_claude / consult_claude_followup
   |     spawns ONE headless        spawns ONE isolated `claude -p` child
   |     `vibe` child in an         (zero tools, zero MCP mounts, no
   |     allowlisted workdir         settings sources, prompt on stdin)
   v                                                   v
 vibe edits files / writes export.json          claude analyzes / answers with
 as evidence                                      a DECISION line + session id
```

The loop is broken twice: the spawned children get `TANDEM_CHILD=1` (tandem
refuses to act inside its own children) *and* their tool surface is stripped
(`--disabled-tools` for MCP servers and built-in connectors on the vibe side;
`--tools ""`, zero MCP config and `--setting-sources ""` on the claude side).

## Status

Work-package development toward v0.1.0. The protocol, hygiene layer (WP2),
both tool sets (WP3/WP4) and install tooling (WP5) are implemented and tested;
live cross-CLI acceptance (WP6) is the remaining gate. The result-JSON
contracts are pinned by fixtures: claude's quota-wall capture is live; the
claude success fixture and both vibe export fixtures are synthesized until
the live captures land.

## Roles

| Mount | Role | Tools | Spawns |
| --- | --- | --- | --- |
| Vibe (user config) | thinker | `consult_claude`, `consult_claude_followup`, `bridge_status` | `claude -p` |
| Claude Code (user scope) | worker | `delegate_to_vibe`, `bridge_status` | `vibe` (headless) |

Consult isolation: each `consult_claude` child runs with zero built-in tools, a zero-MCP
config, no settings sources (no hooks, no CLAUDE.md discovery), and a working directory
validated against this instance's `TANDEM_ALLOWED_ROOTS` - an unset `cwd` means the
server's own working directory, validated by the same rule, and
`consult_claude_followup` reuses and re-validates the consult's registered `cwd`.

## Compatibility matrix

| Component | Baseline tested | Notes |
|---|---|---|
| Claude Code | 2.1.321 (`--resume` probed on 2.1.322) | headless `claude -p`; `--tools ""`; `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`; `--session-id` / `--resume` (resume resolved ids cwd-agnostically in the 2.1.322 probe; the registry still pins the consult cwd) |
| Mistral Vibe | 3.1.1 | headless `vibe -p`; `--prompt-file`; `--time-limit`; `--output-dir` + export.json |
| Python | 3.13 | uv-managed, no build backend (`PYTHONPATH=src`) |
| mcp SDK | 2.3.0 | `MCPServer` (FastMCP renamed), stdio transport |

## Install

Requires: Python 3.13, [uv](https://docs.astral.sh/uv/), `vibe` and `claude`
on PATH.

```sh
scripts/install.sh --roots "$HOME/informatik"          # colon-separated roots
```

- mounts the thinker role in Vibe and the worker role in Claude Code (user
  scope); idempotent (old `tandem` mounts are replaced)
- `--no-vibe` / `--no-claude` mount one side only
- `--state-dir DIR` overrides the state dir (default `~/.cache/tandem`)
- the roots you pass ARE the trust grant: delegations may only target
  directories under them (never the home directory itself or its ancestors,
  never world-writable directories)
- the vibe mount pins the host's MCP timeouts (`--tool-timeout-sec 1860` =
  1800s consult ceiling + headroom, `--startup-timeout-sec 30`) - without
  them vibe's ~60s tool default would cut every long consult; the doctor
  re-checks what is actually written
- Claude Code's permission system gates MCP tool calls: tandem does not edit
  your permissions. To let `delegate_to_vibe` run without per-call
  confirmation, add its rule to `~/.claude/settings.json` under
  `permissions.allow` (e.g. `"mcp__tandem__delegate_to_vibe"` and
  `"mcp__tandem__bridge_status"`); leaving it out just means Claude asks
  before each delegation

```sh
scripts/uninstall.sh          # removes both mounts, keeps the state dir
```

### Diagnose

```sh
uv run python scripts/doctor.py
```

Checks the venv, tandem import, both CLIs, both mounts (roles, state dirs,
roots - mounted roots are re-validated through the server's own home/ancestor
rule, and the thinker/worker grants are compared with each other),
state-dir permissions, the master lock (the real lock file when the state
dir exists), the consult counter against its cap, registry and
run/session/task inventories. Read-only; exit 0 = healthy (warnings
allowed), exit 1 only when tandem could not start on this machine.

## The integration contract

### consult_claude (Vibe asks Claude)

One isolated `claude -p` child per call: structured persona prompt on stdin,
`--output-format json` parsed tolerantly, `--tools ""`, strict zero-MCP
config, `--setting-sources ""` (no settings/hooks/CLAUDE.md discovery; auth
still from the operator's credentials or `ANTHROPIC_*`/`CLAUDE_*` env).

- `mode="decide"` forces a final `DECISION: <one sentence>` line, extracted
  into the `decision` field; `mode="think"` is open analysis.
- The answer ends with `session_id: <id>`; pass it to
  `consult_claude_followup` to continue the thread. Followups always run in
  decide mode: every followup answer targets a `DECISION:` line and the
  `decision` field is set whenever Claude complies. Ids live in tandem's
  registry for 24h after their last use (each followup refreshes the
  window) - an expired or unknown id answers `unknown_session` and the copy
  tells the caller to start a fresh consult, never retry the id.
- `cost_usd` is *this call's* share (Claude reports cumulative totals; tandem
  stores the cumulative figure and returns the delta).
- Followups resume in the consult's registered `cwd` (pinned in the registry,
  re-validated against the allowlist). Live probes on claude 2.1.322 showed
  `--resume` resolving ids cwd-agnostically, so the pin keeps the child
  environment consistent rather than making resume work - and an id Claude
  no longer knows fails out-of-band ("No conversation found" on stderr, no
  result object) and classifies `unknown_session` like any dead id.
- Caps: the rolling-24h consult budget (`TANDEM_DAILY_CONSULT_CAP`, default
  40) is consumed atomically under the state lock, cross-instance; consults
  additionally share `TANDEM_MAX_CHILDREN` (default 2) slots with delegations.
  A consumed budget unit is never refunded - failed, timed-out and
  quota-walled attempts included.

Failure taxonomy (`error_kind` in every error payload): `quota_terminal`
(Anthropic usage wall, includes Claude's reset text), `auth_expired`,
`child_error`, `child_crash` (vibe child died without export evidence),
`terminated` (vibe child killed by design, distinct from `child_error`),
`timeout`, `empty_stdout`, `bad_json`, `bad_response`, `unknown_session`,
`invalid_cwd`, `invalid_arguments`, `state` (state-dir damage, e.g. a
delegation run-dir could not be staged), `state_lock_busy` (the master lock
stayed held beyond the consult timeout), `cap_exceeded`, `loop_guard`,
`spawn_failed`. Unexpected errors surface with the fallback kind
`tandem_error`.

### delegate_to_vibe (Claude Code asks Vibe)

One headless vibe child per call against an explicit `cwd` (required,
allowlist-enforced, never home or world-writable). The task becomes a spec
file (goal, context, and a six-line REPORT contract) staged `0600` under
tandem's private state dir - the prompt never touches argv. Vibe runs with
`--prompt-file`, `--output-dir` (evidence lands next to the spec),
`--time-limit` (vibe exits gracefully and writes `export.json`), `--agent
accept-edits|smart-approve`, `--disabled-tools re:mcp_.* re:connector_.*`,
and `--max-price` when the operator sets a spend cap.

- Honest `export.json` mapping: `finished` -> `completed`; the limit-family
  stop reasons (`deadline`, `turn_limit`, `token_limit`, `price_limit`,
  `length`) -> `status: partial`, *not* an error; `terminated` and error-ish
  stop reasons raise; a missing/unparseable export is `child_crash`; a
  success-shaped export with a nonzero exit code is distrusted.
- The answer carries vibe's structured report; an empty report falls back to
  the export's own answer field; a hard kill leaves `timed_out: true` and a
  warning instead of pretending to be clean, and the timeout copy tells the
  caller the workdir may carry partial edits.
- Delegations consume child slots but NOT the consult budget: the budget
  prices Claude asks; vibe runs are already the local CLI.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TANDEM_ROLE` | (none - refuses to start) | `thinker` or `worker` |
| `TANDEM_LOG_DIR` | `~/.cache/tandem` | state dir (0700; files 0600) |
| `TANDEM_ALLOWED_ROOTS` | server cwd | colon-separated trust grant |
| `TANDEM_MAX_CHILDREN` | 2 | cross-instance child slots (consults + delegations) |
| `TANDEM_DAILY_CONSULT_CAP` | 40 | rolling-24h claude consult budget |
| `TANDEM_DELEGATE_TRUST` | true | run delegated vibe with `--trust`: repo `.vibe` config and hooks load and execute |
| `TANDEM_DELEGATE_MAX_PRICE` | unset | per-run vibe `--max-price` cap (USD) |
| `TANDEM_DELEGATE_RESULT_CAP_BYTES` | 30000 | delegation answer cap; the full report spills to the run dir |
| `TANDEM_TASK_TTL_H` | 24 | delegation run-dir retention hours (floor 2: a max-length run is live 3600s plus headroom; a shorter TTL could sweep it) |
| `TANDEM_KILL_GRACE_S` | 10 | SIGTERM -> SIGKILL patience |
| `TANDEM_STDOUT_CAP_BYTES` | 10 MiB | per-child stdout cap (a truncated parse reports it) |
| `TANDEM_STDERR_KEEP_BYTES` | 8 MiB | per-child stderr tail kept on failures |
| `TANDEM_LOG_CONTENT` | false | reserved: content logging is not yet applied by any tool |
| `TANDEM_CLAUDE_BIN` / `TANDEM_VIBE_BIN` | `claude` / `vibe` | binary paths |

## Security model

- **Trust grant = the allowlist.** Downstream of choosing a repo, project
  hooks and config are trusted by design. Home and its ancestors are refused
  everywhere: at server start, as `TANDEM_ALLOWED_ROOTS` entries (and at
  install time by `install.sh`, which validates against the same rule).
  World-writable directories are refused as delegation targets at call time.
- **Grant breadth is a recorded decision.** The documented install grants
  `$HOME/informatik` - a whole projects tree, not a single repo - so any
  repository under it becomes a valid delegation target the moment it is
  cloned. That breadth is deliberate: the boundary this project relies on is
  the allowlist plus the review gates, not per-repo individuation of grants.
  Narrow it per-project by re-running `install.sh --roots <single-repo>`
  whenever that trade stops being comfortable.
- **Loop guard, two rings:** `TANDEM_CHILD=1` in every spawned child (tandem
  tools refuse to act), plus tool-surface stripping in the child argv.
- **Isolation:** prompts go via stdin / prompt-file, never argv; children run
  in their own process groups with SIGTERM -> grace -> SIGKILL escalation and
  a detached watchdog that reaps groups when a server dies.
- **Minimized environment:** children inherit only PATH/HOME/TMPDIR/TZ/SSL
  certs/locale plus `ANTHROPIC_*`/`CLAUDE_*` (operator auth/routing),
  `TANDEM_*` minus the role; `VIBE_*` config overrides are stripped.
- **Privacy:** run logs are metadata-only (kinds, sizes, hashes, error
  metadata - no prompt or answer content). Delegation specs and vibe's
  `export.json` + journal copies stay in the `0700` state dir until the TTL
  sweep. Note vibe additionally keeps its own session logs under
  `~/.vibe/logs/session` (retention outside tandem's control), and Claude
  Code consults run against Anthropic's service under the operator's
  credentials.
- **Cost control:** consult budget (rolling 24h, atomic, cross-instance),
  child slots, per-call time limits, optional per-run vibe price cap.

## Development

```sh
uv sync
uv run ruff check && uv run ruff format --check
uv run mypy --strict src
uv run pytest -q
uv run python scripts/smoke_stdio.py            # or SMOKE_ROLE=worker ...
```

Layout: `src/tandem/` (`config`, `state`, `child`, `watchdog`, `proctree`,
`runlog`, `guard`, `runner/` CLI adapters, `spec` prompt templates,
`sessions`, `thinker`, `worker`, `server`, `runtime`, `__main__`),
`tests/unit/` with pinned `tests/fixtures/` for both CLIs' output contracts.

## Limitations

- Developed and live-tested on macOS (process groups, `flock`, watchdog
  semantics); Linux is exercised by CI but has had no manual live run -
  the CI loop is what caught the Linux-only zombie-reaping and
  signal-disposition behavior, so treat Linux as supported-by-CI.
- The watchdog's orphan reaper polls `kill(pid, 0)` and reaps registered
  process groups; a pid or pgid recycled into an unrelated process inside
  the narrow check-to-signal window could receive a stray signal. Accepted
  for a local single-user tool; the registry mirror bounds the window.
- Claude Code applies its own host-side MCP tool timeout to `delegate_to_vibe`.
  The vibe mount pins its timeouts at install (`--tool-timeout-sec 1860`,
  `--startup-timeout-sec 30`, re-checked by the doctor); before pushing
  `max_seconds` toward the 3600s ceiling, check the Claude-side budget too -
  a host-cut call surfaces as a tool error on the Claude side while the run's
  evidence stays in the run dir.
- Claude Code consults need working credentials on the machine; quota walls
  surface as `quota_terminal` with Claude's own reset text.
- Session continuation (followups) is capped at 24h of inactivity by tandem
  policy (a sliding window refreshed by each followup, not a fixed per-session
  age), not by Claude's own session lifetime.
