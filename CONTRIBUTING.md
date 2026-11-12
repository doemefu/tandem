# Contributing to tandem

Small project, strict gate. The bar: every line survives a suspicious
review, the docs never outshine the code, and the contract changes
deliberately.

## Development flow

1. Branch per work package (`wpN-*`), stacked on the previous one when the
   stack is in flight (see git history for the current stack order).
2. One commit per work package, amended as review folds land. Keep the
   commit message honest: what the WP added, what each review round changed.
3. Gates before push (all of them):

```sh
uv run ruff check && uv run ruff format --check
uv run mypy --strict src
uv run pytest -q
```

4. CHANGELOG.md updated in the same commit as any behavioral or
   user-visible change.

## The review gate

A work package is not "done" until ALL of these hold:

1. **Subagent specialist reviews** - independent review agents with distinct
   lenses (implementation critic, security, ops, documentation), run
   read-only against the finished WP. Verdicts land in the PR thread with
   their findings folded before merge.
2. **Platform PR review** - the GitHub PR must go through review (Copilot
   review or a human reviewer when GitHub review tooling is unavailable).
3. **CI green** on the PR.

The stacked-PR batch merges as a unit, in stack order; if one PR's fold
rewrites history, every PR above it is rebased (`git rebase --onto`) with
fresh review if the diff materially changed.

## Live-evidence culture

- Fixtures commit real CLI captures (redacted) over synthesized shapes.
- Behavior claims ("verified live") mean a probe ran against the real
  `claude`/`vibe` binaries on this machine and its result is pinned in a
  test or quoted in the PR thread.
- Live probes are one-off harness scripts kept OUT of the repo; anything
  worth keeping becomes a fixture or a test.
- Adds "works-in-practice" facts (compatibility flags, node quirks) to
  the README matrix or CHANGELOG, not to memory.

## Report contract for delegated work

Delegated implementation tasks must end with the six-line REPORT contract
(see `src/tandem/spec.py`) - that contract is what makes delegation
reviewable, and reviewers of delegated PRs should check both the REPORT
fields and the actual diff.
