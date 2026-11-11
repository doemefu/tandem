# tandem

Two-agent bridge for local coding agents: **Claude Code does the hard thinking and
decision-making, Mistral Vibe implements.** One stdio MCP server, mounted in both
CLIs: Vibe gets the `consult_claude` tools (pure-thinking Claude consults, no file or
repo access), Claude Code gets `delegate_to_vibe` (a well-specified implementation task
handed to a Vibe agent working in the project).

The mechanical leg moves off the Claude subscription — Claude-side subagents draw on
the same weekly quota — and planning/review stays on Claude. Quality rests on plan
discipline plus review gates, not on model claims.

**Status: in development (WP1 bootstrap). The tools, mounts, install scripts and the
integration contract land by v0.1.0.**

## Roles

| Mount | Role | Tools |
|---|---|---|
| Vibe (user-scope `[[mcp_servers]]`) | `TANDEM_ROLE=thinker` | `consult_claude`, `consult_claude_followup`, `bridge_status` |
| Claude Code (user-scope MCP) | `TANDEM_ROLE=worker` | `delegate_to_vibe`, `bridge_status` |

## Compatibility matrix

| Component | Baseline tested | Notes |
|---|---|---|
| Claude Code | 2.1.321 | headless `claude -p`; `--tools ""`; `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`; `--session-id` / `--resume` |
| Mistral Vibe | 3.1.1 | headless `vibe -p`; `--prompt-file`; `--time-limit`; `--output-dir` + export.json |
| Python | 3.13 | uv-managed, no build backend (`PYTHONPATH=src`) |
| mcp SDK | 2.3.0 | `MCPServer` (FastMCP renamed), stdio transport |

The full integration contract (tool schemas, spec template, statuses, security model)
is published with v0.1.0.

## Build and test

```bash
uv sync --locked
uv run ruff check && uv run ruff format --check
uv run mypy --strict src
uv run pytest
```

## License

MIT, see [LICENSE](LICENSE).
