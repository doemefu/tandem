"""Vibe adapter: delegate argv construction and export.json parsing.

Both fixtures are synthesized from the documented export.json shape and
research on vibe 3.1.1 (stop_reason values verified via --help output);
WP6's live window replaces them with real captures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tandem.config import Config
from tandem.runner.vibe_runner import build_delegate_args, parse_export

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
    assert argv[11:15] == ["--disabled-tools", "re:mcp_.*", "--disabled-tools", "re:connector_.*"]
    assert argv[15:17] == ["--max-price", "0.5"]
    assert argv[17:] == ["--trust"]
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
    assert argv[-1] == "re:connector_.*"
    assert "--trust" not in argv
    assert "--max-price" not in argv
    assert argv[7:9] == ["--time-limit", "90"]
    assert argv[10] == "smart-approve"


def test_parse_export_success_fixture() -> None:
    outcome = parse_export(FIXTURES / "vibe_export_success.json")
    assert outcome is not None
    assert outcome.stop_reason == "finished"
    assert outcome.session_id == "c47104a2-77e6-4bd0-a2a7-93f805e70d00"
    assert outcome.cost_usd == pytest.approx(0.0712)
    assert outcome.raw is not None


def test_parse_export_deadline_fixture() -> None:
    outcome = parse_export(FIXTURES / "vibe_export_deadline.json")
    assert outcome is not None
    assert outcome.stop_reason == "deadline"
    assert outcome.stop_reason not in ("finished",)
    assert outcome.session_id is not None


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
