# tandem

An MCP bridge that lets **Claude Code** and **Mistral Vibe** work together on one
machine without looping or fighting: **Claude Code does the thinking, Vibe does
the implementing.**

```
Claude Code (decisions)                             Vibe (implementation)
   |                                                   |
   |  worker role MCP mount                            |  thinker role MCP mount
   v                                                   v
   +--------------------> tandem <---------------------+
   |  delegate_to_vibe        consult_claude / consult_claude_followup
   |     spawns ONE headless        spawns ONE isolated `claude -p` child
   |     `vibe` child in an         (zero tools, zero MCP mounts, no
   |     allowlisted workdir         settings sources, prompt on stdin)
   v                                                   v
 vibe edits files / writes export.json          claude analyzes / answers with
 as evidence                                      a DECISION line + session id
```

The loop is broken, with boundaries measured live rather than claimed: the
spawned children get `TANDEM_CHILD=1` (tandem refuses to act one level deep),
and their tool surface is stripped (`--disabled-tools` for MCP servers,
connectors, tool discovery and the tandem tool names on the vibe side;
`--tools ""`, zero MCP config and `--setting-sources ""` on the claude side).
A mounted machine still spawns the tandem server inside a delegated child
(vibe's MCP client behavior), and deep hops do not carry `TANDEM_CHILD` - the
security model below documents exactly where that leaves the boundary.

## Status

v0.1.0: the protocol, hygiene layer, both tool sets and the install tooling
are implemented and tested. The live matrix has run end to end - a live
consult through the thinker pipeline, a live delegation through the worker
pipeline - and the result-JSON contracts are pinned by fixtures: the claude
captures are live, the vibe export captures are live (sanitized of
machine/account noise). See [CHANGELOG.md](CHANGELOG.md) for the
work-package history.

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

Role names say what the mount serves its host, not which model thinks or
implements: Vibe mounts the thinker role because `consult_claude` brings in
Claude's thinking; Claude Code mounts the worker role because
`delegate_to_vibe` hands work to Vibe. `bridge_status` ships with both
roles: it reports the role, lazily probed Claude Code/Vibe versions, caps
and running children.

## Compatibility matrix

| Component | Baseline tested | Notes |
|---|---|---|
| Claude Code | 2.1.321 (`--resume` probed on 2.1.322) | headless `claude -p`; `--tools ""`; `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`; `--session-id` / `--resume` (resume resolved ids cwd-agnostically in the 2.1.322 probe; the registry still pins the consult cwd) |
| Mistral Vibe | 3.1.1 | headless `vibe -p`; `--prompt-file`; `--time-limit`; `--output-dir` + export.json |
| Python | 3.13 | uv-managed, no build backend (`PYTHONPATH=src`) |
| mcp SDK | 2.3.0 | `MCPServer` (FastMCP renamed), stdio transport |

## Install

From a clone of this repo. Requires: Python 3.13,
[uv](https://docs.astral.sh/uv/), `vibe` and `claude` on PATH. macOS is the
live-tested platform; Linux is exercised by CI (see Limitations).

```sh
scripts/install.sh --roots "$HOME/informatik"          # colon-separated roots
```

- mounts the thinker role in Vibe and the worker role in Claude Code (user
  scope); idempotent - re-running the same command after a pull replaces
  the mounts with the current checkout
- `--no-vibe` / `--no-claude` mount one side only
- `--state-dir DIR` overrides the state dir (`TANDEM_LOG_DIR` as mounted;
  default `~/.cache/tandem`)
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

### Diagnose

```sh
uv run python scripts/doctor.py
```

Read-only checks:

- venv, tandem import, both CLIs on PATH
- both mounts: roles, state dirs, roots - mounted roots re-validated
  through the server's own home/ancestor rule, thinker/worker grants
  compared with each other
- state-dir permissions, the master lock (the real lock file when the
  state dir exists), the consult counter against its cap
- registry liveness and run/session/task inventories

Exit contract: 0 = healthy (warnings allowed), 1 only when tandem could
not start on this machine.

### First use

The mounts load in sessions started after the install - restart Claude
Code and Vibe if they were running, then from either side:

- Vibe: call `consult_claude` on a real question (expect a `DECISION:` line
  in decide mode and a `session_id:` trailer to feed
  `consult_claude_followup`).
- Claude Code: call `delegate_to_vibe` on a small task in a repo under your
  roots (expect vibe's REPORT block and a `completed` / `partial` status -
  see the mapping below).

If a tool is missing or errors, re-run the doctor and `bridge_status`.

### Uninstall

```sh
scripts/uninstall.sh          # removes both mounts, keeps the state dir
```

## The integration contract

Failure taxonomy - every error payload from either pipeline carries an
`error_kind`: `quota_terminal` (Anthropic usage wall, includes Claude's
reset text), `auth_expired`, `child_error`, `child_crash` (vibe child died
without export evidence), `terminated` (vibe child killed by design,
distinct from `child_error`), `timeout`, `empty_stdout`, `bad_json`,
`bad_response`, `unknown_session`, `invalid_cwd`, `invalid_arguments`,
`state` (state-dir damage, e.g. a delegation run-dir could not be staged),
`state_lock_busy` (the master lock stayed held beyond the consult timeout),
`cap_exceeded`, `loop_guard`, `spawn_failed`. Unexpected errors surface
with the fallback kind `tandem_error`.

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

- Failures use the shared taxonomy above.

### delegate_to_vibe (Claude Code asks Vibe)

One headless vibe child per call against an explicit `cwd` (required,
allowlist-enforced, never home or world-writable). The task becomes a spec
file (goal, context, and a six-line REPORT contract) staged `0600` under
tandem's private state dir - the prompt never touches argv. Vibe runs with
`--prompt-file`, `--output-dir` (evidence lands next to the spec),
`--time-limit` (vibe exits gracefully and writes `export.json`), `--agent
smart-approve|accept-edits`, and five `--disabled-tools` patterns
(`re:mcp_.*`, `re:connector_.*`, `search_tool_functions` plus the tandem
tool names - see the security model), and `--max-price` when the operator
sets a spend cap.

- Honest `export.json` mapping (pinned by live vibe 3.1.1 captures):

  | vibe's exit / export shape | tandem result |
  | --- | --- |
  | `stop_reason: null`, top-level `outcome: "finished"`, exit 0 | `status: completed` |
  | `stop_reason: "interrupted"`, `outcome: "deadline"`, exit 3 (expected) | `status: partial` - not an error |
  | other limit-family stops (`deadline`, `turn_limit`, `token_limit`, `price_limit`, `length`) | tolerated; the payload's `outcome` tells which limit fired |
  | `terminated` / error-ish stop reasons | raise |
  | success-shaped export with an unexpected nonzero exit | distrusted (the child result wins) |
  | exit without a parseable export | `child_crash` (or `timeout` when the hard kill raced the export write) |

  A limit stop exiting with anything other than 0/3 stays a partial but
  surfaces a warning.
- The answer carries vibe's structured report; an empty report falls back to
  the export's own answer field; a hard kill leaves `timed_out: true` and a
  warning instead of pretending to be clean, and the timeout copy tells the
  caller the workdir may carry partial edits.
- Delegations consume child slots but NOT the consult budget: the budget
  prices Claude asks; vibe runs are already the local CLI.
- Failures use the shared taxonomy above.

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
- **Loop guard rings, as actually verified live (vibe 3.1.1, mounted
  machine):** `TANDEM_CHILD=1` in every tandem-spawned CLI child makes the tandem tools
  refuse to act one level deep - but a vibe child spawns its OWN MCP servers
  (the mounted tandem server starts inside a delegated child at client
  startup, `server_started role=thinker` in the runlog), and vibe does not
  pass a tandem-spawned child's environment on to those servers, so
  `TANDEM_CHILD` does not survive that hop. What holds there is the
  tool-surface strip, hardened after a live probe found a child reaching the
  nested server THROUGH `search_tool_functions` tool discovery despite
  `re:mcp_.*` (the pre-hardening probe ran one real paid consult through the
  nested server, confined by the mount's cwd allowlist and billed to the
  same consult budget): delegated children now also disable
  `search_tool_functions` and the tandem tool names explicitly. The
  post-hardening live probe ends `REPORT_STATUS: blocked` with the nested
  server present but no tool call landing on it. The strip remains
  best-effort: treat the cwd allowlist and the consult budget - both
  verified live as the boundaries that held - as the load-bearing controls.
- **Isolation:** prompts go via stdin / prompt-file, never argv; children run
  in their own process groups with SIGTERM -> grace -> SIGKILL escalation and
  a detached watchdog that reaps groups when a server dies.
- **Minimized environment:** children inherit only
  PATH/HOME/USER/LOGNAME/TMPDIR/TZ/SSL certs/locale plus
  `ANTHROPIC_*`/`CLAUDE_*` (operator auth/routing), `TANDEM_*` minus the
  role; `VIBE_*` config overrides are stripped. `USER`/`LOGNAME` matter
  beyond cosmetics: claude's macOS credentials live in the login keychain
  and the CLI resolves them through the user identity (live-verified:
  without `USER`, every consult died with `auth_expired`).
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

`.github/workflows/ci.yml` runs the same checks.

Layout: `src/tandem/` (`config`, `state`, `child`, `watchdog`, `proctree`,
`runlog`, `guard`, `runner/` CLI adapters, `spec` prompt templates,
`sessions`, `errors`, `thinker`, `worker`, `server`, `runtime`,
`__main__`), `tests/unit/` with pinned `tests/fixtures/` for both CLIs'
output contracts.

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

## License

MIT - see [LICENSE](LICENSE).
