"""Vibe adapter: delegate argv construction and export.json parsing.

Both fixtures are live vibe 3.1.1 captures (WP6), sanitized: the real
exports carry a large machine-specific config dump (model catalog,
providers, account identifiers) that is reduced to the delegation-relevant
keys. A clean finish writes stop_reason null with outcome "finished";
the graceful --time-limit stop writes stop_reason "interrupted" with
outcome "deadline" and exit code 3.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tandem.config import Config
from tandem.runner.vibe_runner import STOP_REASON_LIMITS, build_delegate_args, parse_export

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _cfg(*, delegate_trust: bool = True) -> Config:
    return Config.from_env({"TANDEM_ROLE": "worker", "TANDEM_DELEGATE_TRUST": "true" if delegate_trust else "false"})


def test_build_delegate_args_full_isolation() -> None:
    argv = build_delegate_args(
        _cfg(),
        workdir=Path("/Users/dom/proj"),
        prompt_file=Path("/state/tasks/delegate-abc/prompt.md"),
        output_dir=Path("/state/tasks/delegate-abc"),
        max_seconds=600.0,
        agent="accept-edits",
        max_price_usd=0.5,
    )
    assert argv[0] == "vibe"
    assert argv[1:3] == ["--workdir", "/Users/dom/proj"]
    assert argv[3:5] == ["--prompt-file", "/state/tasks/delegate-abc/prompt.md"]
    assert argv[5:7] == ["--output-dir", "/state/tasks/delegate-abc"]
    assert argv[7:9] == ["--time-limit", "600"]
    assert argv[9:11] == ["--agent", "accept-edits"]
    # MCP servers AND built-in connectors are out of reach for a delegated run.
    assert argv[11:21] == [
        "--disabled-tools",
        "re:mcp_.*",
        "--disabled-tools",
        "re:connector_.*",
        "--disabled-tools",
        "search_tool_functions",
        "--disabled-tools",
        "re:^(mcp_)?tandem",
        "--disabled-tools",
        "re:^(mcp_tandem\\.)?bridge_status",
    ]
    assert argv[21:23] == ["--max-price", "0.5"]
    assert argv[23:] == ["--trust"]
    assert "-p" not in argv  # --prompt-file alone enters programmatic mode


def test_build_delegate_args_without_trust() -> None:
    argv = build_delegate_args(
        _cfg(delegate_trust=False),
        workdir=Path("/proj"),
        prompt_file=Path("/p.md"),
        output_dir=Path("/out"),
        max_seconds=90.0,
        agent="smart-approve",
    )
    assert argv[-1] == "re:^(mcp_tandem\\.)?bridge_status"
    assert "--trust" not in argv
    assert "--max-price" not in argv
    assert argv[7:9] == ["--time-limit", "90"]
    assert argv[10] == "smart-approve"


def test_parse_export_success_fixture() -> None:
    outcome = parse_export(FIXTURES / "vibe_export_success.json")
    assert outcome is not None
    assert outcome.stop_reason is None  # clean finish: outcome names it, stop_reason is null
    assert outcome.session_id == "ab30482b-a2c9-75c5-9d8a-ca069f5ece04"
    assert outcome.cost_usd == pytest.approx(0.0212462)
    assert outcome.raw is not None
    assert outcome.raw["outcome"] == "finished"
    assert outcome.raw["exit_code"] == 0
    # Isolation-relevant config, pinned from the live post-install capture:
    # the mounted tandem server visibly present in the child config (vibe's
    # client starts it), and the seven-entry disabled list: five delegate
    # patterns + two operator-baseline disables.
    config = outcome.raw["config"]
    assert config["disabled_tools"] == [
        "re:mcp_.*",
        "re:connector_.*",
        "search_tool_functions",
        "re:^(mcp_)?tandem",
        "re:^(mcp_tandem\\.)?bridge_status",
        "ask_user_question",
        "cron",
    ]
    assert [s["name"] for s in config["mcp_servers"]] == ["tandem"]
    assert config["mcp_servers"][0]["tool_timeout_sec"] == 1860.0
    # Sanitization contract: no operator-identifying content survives.
    assert "client_key" not in json.dumps(config)
    assert ".venv/bin/python" not in json.dumps(config)


def test_parse_export_deadline_fixture() -> None:
    outcome = parse_export(FIXTURES / "vibe_export_deadline.json")
    assert outcome is not None
    assert outcome.stop_reason == "interrupted"  # vibe 3.1.1's graceful limit stop
    assert outcome.stop_reason in STOP_REASON_LIMITS
    assert outcome.raw is not None
    assert outcome.raw["outcome"] == "deadline"  # the limit kind lives in outcome
    assert outcome.raw["exit_code"] == 3  # expected on limit stops, not a crash
    assert outcome.session_id == "a2970688-94e9-6841-7ef1-1d4b9091e79c"


def test_parse_export_missing_file_returns_none(tmp_path: Path) -> None:
    assert parse_export(tmp_path / "export.json") is None


def test_parse_export_rejects_non_dict(tmp_path: Path) -> None:
    corrupt = tmp_path / "export.json"
    corrupt.write_text("[1, 2, 3]")
    assert parse_export(corrupt) is None
    corrupt.write_text("not json at all")
    assert parse_export(corrupt) is None


def test_parse_export_harvests_warnings_capped(tmp_path: Path) -> None:
    export = tmp_path / "export.json"
    export.write_text(json.dumps({"stop_reason": "finished", "warnings": [f"w{i}" for i in range(80)]}))
    outcome = parse_export(export)
    assert outcome is not None
    assert len(outcome.warnings) == 50
