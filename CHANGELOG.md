# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); from v0.1.0 onward the project
uses semantic versioning.

## [Unreleased]

### Added

- Repository bootstrap (WP1): uv-managed Python 3.13 project with the pinned toolchain
  (ruff, mypy `--strict`, pytest), CI mirroring homelab-mcp-hub (SHA-pinned actions),
  and the live-captured `claude -p` usage-limit result JSON as the fixture for the
  upcoming result-parser contract.
- Package skeleton `src/tandem`: the entrypoint validates `TANDEM_ROLE`
  ("thinker"/"worker") and refuses to start (exit 2) on a missing or unknown role.
- Runner core (WP2): async child runner (`anyio.open_process` in own session/group,
  SIGTERM -> grace -> SIGKILL escalation, stdout spooling with a flood cap, shielded
  cleanup on host cancellation), main-thread signal choreography, and per-process
  JSONL run log (metadata only; content logging stays off by default).
- Cross-instance state and guards (WP2): 0700/0600 state dir with a single
  never-unlinked master flock, atomic writes with corrupt-file quarantine,
  rolling-24h consult counter, live child registry mirror, `TANDEM_CHILD` loop
  guard, and a detached watchdog process (`python -m tandem.watchdog`) that reaps
  owned child groups when the server dies (out-of-band `kill(pid, 0)` polling -
  kqueue cannot watch ancestors on macOS).
- `bridge_status` tool (WP2): role, lazy probed Claude Code/Vibe versions, caps and
  running children; `scripts/smoke_stdio.py` performs a full host-style handshake
  and bridge_status round trip for both roles.
- Review fold (WP2, GitHub review round): the proctree tests spawned their sleeper
  processes through `asyncio.run()`, leaving them unreaped zombies on Linux's
  pidfd watcher once the loop closed - `pid_alive()` reports zombies as alive, so
  CI was red on every branch in the stack; sleepers are now plain `Popen` with
  explicit reaping. The shutdown handlers swallowed SIGTERM/SIGINT/SIGHUP (the
  serve loop blocks in a stdin read that ignores cooperative cancellation -
  verified live, the server kept serving after SIGTERM until the host's
  SIGKILL); the handler now kills this instance's registered child groups and
  re-terminates the process with the signal's default disposition, so the host
  sees the signal-dead process inside its teardown window and the watchdog owns
  the grace/SIGKILL escalation for stragglers. `run_child`'s cleanup unregisters
  the child from a worker thread and a lock failure there can no longer mask a
  finished child's result (it degrades to a stderr breadcrumb; the watchdog and
  the stale prune sweep the entry).
- Thinker role tools (WP3): `consult_claude` and `consult_claude_followup` run an
  isolated `claude -p` child (zero built-in tools via `--tools ""`, zero MCP mounts,
  prompt on stdin, JSON result parsing with noise tolerance) under the structured
  consult persona with a mandatory `DECISION:` line in decide mode. Failures carry
  `error_kind` taxonomies (`quota_terminal` on the Anthropic usage wall,
  `auth_expired`, `timeout`, `bad_json`, `unknown_session`, ...). Answers end with a
  `session_id:` trailer; followups resume via `--resume` for 24h (registry TTL, ids
  validated as filenames), and cost reporting is per call (cumulative figure stored
  in the registry). The rolling-24h consult budget is consumed atomically under the
  master lock; per-call cost/turn/latency telemetry lands in the JSONL run log.
- Session registry (WP3): persistent continuation records with TTL sweep, corrupt
  file quarantine, and traversal-proof id validation.
- Review fold (WP3): the consult budget consume is now atomic under the master
  flock (a same-pid tmp-name collision in atomic writes was also fixed); the WP2
  `is_cwd_allowed` home check was inverted (it refused every directory inside
  `$HOME` instead of home and its ancestors only) - fixed with accept-path tests;
  consults run with `--setting-sources ""` (no settings/hooks/CLAUDE.md discovery,
  auth unaffected) and an untrusted server cwd now fails closed; `ANTHROPIC_*` and
  `CLAUDE_*` operator env vars pass through to children (gateway/Bedrock auth);
  question over the 64KiB byte cap is rejected (`invalid_arguments`) instead of
  silently truncated, context truncation is surfaced in the runlog; runlog gains
  `consult_refused`/`consult_abandoned` events and spawn failures/cancellations no
  longer dangle a `consult_started`; consult tool annotations no longer claim
  read-only. Note: `tests/fixtures/claude_print_success.json` is synthesized from
  the documented result shape; WP6 acceptance replaces it with a live capture.
- Review fold (WP3, GitHub review round 2): the session registry now pins the
  consult's `cwd` per record and `consult_claude_followup` spawns there (re-validated
  against the allowlist so narrowed roots still fail closed). Live probes on
  claude 2.1.322 showed `--resume` resolving ids cwd-agnostically - the pin is
  environment consistency and insurance, not a resume prerequisite (a "no usable
  cwd" record classifies `unknown_session`). An id Claude no longer knows fails
  out-of-band - empty stdout, rc 1, `No conversation found` on stderr - pinned by
  an inline test string from the live probe and classified
  `unknown_session` (no retry, fresh consult); the same live session captured the
  cumulative-cost contract on a resume (first call 0.0182218 USD -> resumed figure
  0.0213966 USD, delta reported per call; `num_turns` counts the invocation only).
  Auth classification tightened to status 401 plus the canonical login-wall
  phrases ("Not logged in", "Login expired") - a stray "log in" inside an
  unrelated error stays `child_error`.
- Worker role tools (WP4): `delegate_to_vibe` runs one headless vibe child per
  delegation - the task becomes a spec file (goal/context plus a six-line REPORT
  reporting contract) staged 0600 under a private per-run dir in the state tree,
  and vibe gets `--workdir` (required, allowlist-enforced, never home),
  `--prompt-file` (programmatic mode, the prompt never touches argv),
  `--output-dir` (export.json evidence), a graceful `--time-limit` with tandem
  hard-kill headroom, `--agent accept-edits|smart-approve`, and
  `--disabled-tools re:mcp_.*` so the child cannot reach any MCP server. Sandbox
  caps share the cross-instance `TANDEM_MAX_CHILDREN` limit (the rolling-24h
  consult budget is NOT consumed: it prices Claude asks, not local vibe runs).
  `export.json` maps honestly: `finished` -> completed; limit stop_reasons
  (deadline/turn/token/price/length) -> `status: partial` without an error;
  `terminated`/error stop_reasons and a success-shaped export with a nonzero exit
  raise (`terminated`/`child_error`); a missing/unparseable export is
  `child_crash`. Both vibe fixtures are synthesized pending WP6 live captures.
- Review fold (WP4): run-dir TTL sweeping actually deletes delegation directories
  (whole-tree sweep at delegation time and server startup - `sweep_ttl` only ever
  matched loose files, so run dirs were leaking); a hard kill with a usable export
  keeps export-wins semantics but reports `timed_out: true` plus a synthetic
  warning; the timeout error says the workdir may carry partial edits and points
  at the run dir; an empty report falls back to the export's own answer field and
  a completed run with no report anywhere is `bad_response`; `--disabled-tools`
  now also strips built-in connectors (`re:connector_.*`) and an operator
  `TANDEM_DELEGATE_MAX_PRICE` passes `--max-price` per run; the kill wall's
  headroom never drops below 30s; world-writable delegation cwds are refused;
  staging failures raise structured `state` errors with a `delegate_refused`
  event; `TANDEM_ALLOWED_ROOTS` entries equal to home (or its ancestors) refuse
  to start; child-registration failures in the runner now kill the fresh process
  group instead of leaking it, and the registry's in-memory map is
  thread-mutation-safe. `TANDEM_DELEGATE_TRUST` (default on - repo `.vibe`
  config and hooks load and execute for delegated runs) is now disclosed in the
  tool description.
- Review fold (WP4, GitHub review round 2): the default delegate mode is
  `smart-approve` (the plan's value; `accept-edits` remains available for
  edit-only tasks, and its tool copy now says plainly that EVERY shell command
  is denied in that mode - a headless run cannot ask a human for the approval
  those commands need). The six-line REPORT contract is parsed and surfaced as
  structured payload fields (`report_status`, `report_summary`,
  `files_changed`, `commands_run`, `result_paths`, `remaining`); a
  REPORT_STATUS that disagrees with the export-derived status warns and never
  rewrites either side. A live smart-approve delegation verified the contract
  end-to-end and showed the real export.json carries no structured command
  list - so `commands_run` is the child's self-reported audit, carried into
  the `delegate_completed` runlog event together with mode, trust and
  `report_status` (`delegate_started` carries trust, any price cap and the
  result cap). Answers over `TANDEM_DELEGATE_RESULT_CAP_BYTES` (new knob,
  default 30k) spill whole to `report.md` in the run dir while the visible
  answer clips at the cap; contract parsing runs on the full report before
  capping so the structured fields survive intact, cap-truncated stdout
  yields to the export's own complete answer copy before any of that, and a
  failed spill write degrades to clip-without-spill instead of losing the
  run. Export warnings serialize as JSON strings (structured entries were
  Python dict-reprs), the top-level `answer` export key is documented as
  defensive-only (live exports nest it under `outcome`), and
  `TANDEM_TASK_TTL_H` now refuses values below two hours - a max-length
  delegation's run dir is live for 3600s plus the kill headroom while the
  dir mtime sits at staging, so a TTL at the run's own length could sweep
  it.
- Install, doctor and docs (WP5): `scripts/install.sh` mounts the thinker role in
  Vibe (`vibe mcp add`, user config) and the worker role in Claude Code
  (`claude mcp add-json`, user scope) from the repo's uv-managed venv python,
  idempotently and with an explicit `--roots` trust grant (home/ancestor roots
  refused at install time as well as at server start); `scripts/uninstall.sh`
  removes both mounts and keeps the state dir; `scripts/doctor.py` reports
  venv/tandem/CLI health, both mounts (Toml and JSON config parsing), state-dir
  permissions, master flock reachability, registry liveness and
  session/task/run inventories - read-only, warnings-not-failures semantics;
  README rewritten as the integration contract (roles, tool semantics, config
  table, security model incl. vibe's own `~/.vibe/logs/session` transcript
  retention and claude-consult privacy, limitations).
- Review fold (WP5): the installer's home-root check was inverted both ways -
  it refused every directory under `$HOME` (the README's own install example
  died with exit 2) while accepting ancestors of home such as `/Users` - roots
  are now resolved once and refused exactly when home is at or under the root,
  mirroring `tandem.config._reject_home_roots`; `--roots` splits on colons only
  (whitespace inside entries preserved, matching the server's PATH-like
  parsing), missing option values report usage instead of `set -u` noise,
  paths that cannot be embedded in the claude mount JSON (quotes, backslashes)
  are refused before any write, binary overrides are checked as given and
  pinned to absolute paths when resolvable (a GUI-launched MCP host sees a
  different PATH than the terminal), and `--help` prints its header via a
  marker rule instead of a line range. `uninstall.sh` distinguishes
  nothing-mounted from failed removals (a mount still listed after a failed
  remove is a hard error). The doctor probes the real master lock when the
  state dir has one (previously a throwaway file), fails on a state dir that
  exists as a non-directory, expands `~` in mount values and compares both
  mounts with each other instead of against the shell env (a matching install
  now reports zero phantom root warnings; the invoking shell normally sets no
  roots - the mounts carry them), re-validates mounted roots through the
  server's own rule, shows the consult counter against its cap (shell >
  agreeing mounts > default) and warns when the two mounts set different
  caps, reads the rolling counters read-only (no quarantine renames), and
  tolerates malformed registry rows; its exit contract (0 = healthy with
  warnings, 1 = tandem could not start) is now stated accurately. The README
  gains the complete `error_kind` taxonomy (`child_crash`, `terminated`,
  `state`, `state_lock_busy`, fallback `tandem_error`), the no-refund budget
  policy, the followup decide-mode disclosure (also in the followup tool
  description), config rows for `TANDEM_STDOUT_CAP_BYTES` /
  `TANDEM_STDERR_KEEP_BYTES`, the TMPDIR env passthrough and the sliding
  (idle-window) 24h session TTL; world-writable refusal is documented as the
  call-time delegation rule it is. New regression tests pin the installer
  validation contract (rejection paths, whitespace-preserving roots, help
  output) and the doctor's install-consistency behavior (drift, home-rule
  mounts, broken state dir, missing CLIs, counter display). A live install
  on vibe 3.1.1 then caught two integration bugs no static review could:
  `vibe mcp add --arg -m` parses `-m` as a flag (the installer passes
  `--arg=-m` in value form), and the user config stores servers as a named
  `[[mcp_servers]]` array of tables (the doctor parses the real shape,
  keeping tolerance for the keyed-table form); a fully mounted machine now
  reports healthy with zero warnings.
- Review fold (WP5, GitHub review round 2): `install.sh` pins the vibe host's
  own MCP timeouts into the thinker mount (`--tool-timeout-sec 1860` = the
  1800s consult ceiling plus headroom, `--startup-timeout-sec 30`; both flags
  verified against `vibe mcp add --help`) - vibe's ~60s tool default would cut
  every long consult; the doctor now re-checks `tool_timeout_sec` /
  `startup_timeout_sec` as written in the vibe config (missing or low values
  warn with the reinstall advice), and re-running the installer live-verified
  the persisted shape (`tool_timeout_sec = 1860.0` in the user config). The
  README records the wide-grant decision (`$HOME/informatik` breadth is
  deliberate - the boundary is allowlist + review gates, not repo
  individuation), documents the followup cwd pin and the "No conversation
  found" classification in the consult contract, adds the
  `~/.claude/settings.json` permissions allow-list note (tandem never edits
  permissions; the absence just means per-call confirmation), honest platform
  wording (macOS live-tested, Linux exercised by CI - the CI loop caught the
  Linux-only zombie/signal behavior), the watchdog pid-reuse caveat, and the
  Claude-host tool-timeout caution for delegations pushed toward 3600s. New
  process artifacts: `CLAUDE.md` (agent guidance incl. gates and the
  fail-closed/metadata-only invariants) with `AGENTS.md` as a symlink to it,
  and `CONTRIBUTING.md` (the review gate: subagent specialist reviews + PR
  review + CI green before merge, the one-commit-per-WP amend discipline, and
  the live-evidence culture). Also from this round: `uninstall.sh`'s vibe-side
  failure detection was dead code (vibe 3.1.1 has no `mcp list`, live-probed),
  so a failed `vibe mcp remove` silently printed "(nothing to remove)" - the
  guard now resolves the vibe config file itself (both array-of-tables and
  keyed-table shapes), a residual tandem entry after a failed remove is a
  hard error, and fake-CLI uninstall tests pin the contract; the doctor's
  consult window imports the counter's own constant instead of duplicating
  86400, and the install-test module doc no longer overclaims that every case
  aborts in validation.
