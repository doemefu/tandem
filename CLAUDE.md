# tandem - agent guidance

tandem is a small stdio MCP bridge (Python 3.13, uv-managed, `PYTHONPATH=src`):
Claude Code does decisions, Mistral Vibe implements. Vibe mounts the thinker
role (`consult_claude`, `consult_claude_followup`); Claude Code mounts the
worker role (`delegate_to_vibe`). Read README.md first - it is the
integration contract and security model, and it is kept accurate.

## Commands

```sh
uv sync --locked
uv run ruff check && uv run ruff format --check
uv run mypy --strict src
uv run pytest -q
uv run python scripts/smoke_stdio.py
uv run python scripts/doctor.py
```

All four gates (ruff, format, mypy --strict, pytest) must pass before any
commit is pushed. CI runs the same set on Linux; macOS is the live-tested
platform - do not "fix" platform behavior (signal disposition, pidfd,
flock) without understanding why it is written the way it is.

## Project rules that are easy to break by accident

- Fail closed: unknown/unparseable input in the state dir is quarantined or
  ignored, never guessed at. Trust boundaries (allowed roots, world-writable
  targets, child env) refuse, they never warn-and-continue.
- Run logs are metadata-only: no prompt or answer content, ever.
- Children are adversarial input sources: their stdout/exports parse
  tolerantly and are never trusted over exit code evidence.
- Fixtures pin real CLI output shapes. Live captures get committed as
  fixtures with secrets redacted (credential metadata, machine paths where
  they identify the operator), then tests pin the parser against them.
- Every user-visible or behavioral change lands in CHANGELOG.md in the same
  commit, Keep-a-Changelog style, honest about limitations.
- One commit per work package; review folds fold INTO that commit (amend),
  and the commit message records what the fold changed. The branch stack
  (wp2 <- wp3 <- wp4 <- wp5 <- wp6) is rebased as a unit - force-push with
  `--force-with-lease`, never merge into a WP branch.
- The review gate (see CONTRIBUTING.md) is not optional: implementation
  critic + security/ops subagent reviews per work package, plus the
  platform PR review, before anything merges to main.
- Delegation answers cap at `TANDEM_DELEGATE_RESULT_CAP_BYTES` with the
  full report spilled to the run dir; consult answers end with a
  `session_id:` trailer. Keep new payload fields documented in README's
  contract when they change.

## Where things live

`src/tandem/` - `config` (env knobs + trust rules), `state` (locks, atomic
writes, sweeps), `child`/`watchdog`/`proctree` (process supervision),
`runner/` (claude/vibe CLI adapters), `spec` (prompt templates + REPORT
contract parser), `sessions` (followup registry), `thinker`/`worker` (the
tools), `server`/`runtime`/`__main__`. Tests are unit-level with faked
children; live probes belong in a scratch harness, never in the suite.
