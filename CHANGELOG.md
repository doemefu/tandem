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
