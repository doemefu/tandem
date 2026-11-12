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
