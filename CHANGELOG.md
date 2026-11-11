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
