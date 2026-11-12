"""Claude consult argv construction and tolerant result-JSON parsing.

Both committed fixtures pin the parser: the quota-wall capture
(live-captured in WP1), the synthesized success fixture (to be
replaced by a live capture during WP6 acceptance), and the live
followup capture of a resumed session (WP3 review round).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tandem.child import ChildResult
from tandem.config import Config
from tandem.runner.claude_runner import build_consult_args, parse_claude_result

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _cfg() -> Config:
    return Config.from_env({"TANDEM_ROLE": "thinker"})


def _child(
    stdout: bytes = b"", stderr: bytes = b"", *, timed_out: bool = False, truncated: bool = False
) -> ChildResult:
    return ChildResult(
        returncode=0,
        signal=None,
        timed_out=timed_out,
        stdout=stdout,
        stdout_truncated=truncated,
        stderr=stderr,
        stderr_truncated=False,
        duration_s=1.2,
        first_byte_s=0.2,
    )


def test_build_consult_args_new_session() -> None:
    argv = build_consult_args(_cfg(), session_id="b07e0c55-9f6a", resume_id=None)
    assert argv[0] == "claude"
    assert argv[1:4] == ["-p", "--output-format", "json"]
    assert argv[4:11] == [
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--setting-sources",
        "",
    ]
    assert argv[11:] == ["--session-id", "b07e0c55-9f6a"]
    assert "--resume" not in argv


def test_build_consult_args_resume() -> None:
    argv = build_consult_args(_cfg(), session_id=None, resume_id="b07e0c55-9f6a")
    assert argv[-2:] == ["--resume", "b07e0c55-9f6a"]
    assert "--session-id" not in argv


def test_build_consult_args_exclusive_args() -> None:
    cfg = _cfg()
    with pytest.raises(ValueError, match="exactly one"):
        build_consult_args(cfg, session_id=None, resume_id=None)
    with pytest.raises(ValueError, match="exactly one"):
        build_consult_args(cfg, session_id="a07e0c55-9f6a", resume_id="b07e0c55-9f6a")


def test_parse_success_fixture() -> None:
    raw = (FIXTURES / "claude_print_success.json").read_bytes()
    outcome = parse_claude_result(_child(raw))
    assert outcome is not None
    assert outcome.is_error is False
    assert outcome.session_id == "b07e0c55-9f6a-4f9e-8c8a-1a2b3c4d5e6f"
    assert outcome.num_turns == 2
    assert outcome.cost_usd == pytest.approx(0.1245)
    assert outcome.answer is not None
    assert outcome.answer.startswith("1. Analysis")
    assert outcome.answer.endswith("cache the results.")


def test_parse_followup_fixture() -> None:
    """Live capture of a resumed consultation (claude 2.1.322).

    ``total_cost_usd`` is cumulative across the resumed session; ``num_turns``
    counts this invocation only.  The registry pins the cumulative figure and
    the tool reports the delta.
    """
    raw = (FIXTURES / "claude_print_followup.json").read_bytes()
    outcome = parse_claude_result(_child(raw))
    assert outcome is not None
    assert outcome.is_error is False
    assert outcome.session_id == "7f3d1a2c-9b4e-4c5d-8a6f-1e2b3c4d5e6f"
    assert outcome.num_turns == 1
    assert outcome.cost_usd == pytest.approx(0.0213966)
    assert outcome.answer is not None
    assert outcome.answer.strip() == "beta"


def test_parse_quota_fixture() -> None:
    raw = (FIXTURES / "claude_print_usage_limit.json").read_bytes()
    outcome = parse_claude_result(_child(raw))
    assert outcome is not None
    assert outcome.is_error is True
    assert outcome.api_error == "usage_limit_reached"
    assert outcome.api_error_status == 429
    assert outcome.session_id == "227f6aaf-c69a-4f87-90c7-0f91ba3ba50d"
    assert outcome.num_turns == 1


def test_parse_survives_noise_lines_around_json() -> None:
    payload = json.loads((FIXTURES / "claude_print_success.json").read_text())
    noisy = b"Update available: 1.2.3\n\n" + json.dumps(payload).encode() + b"\nsome trailing note\n"
    outcome = parse_claude_result(_child(noisy))
    assert outcome is not None
    assert outcome.is_error is False
    assert outcome.answer is not None


def test_parse_empty_stdout_returns_none() -> None:
    assert parse_claude_result(_child(b"")) is None


def test_parse_truncated_garbage_returns_none() -> None:
    assert parse_claude_result(_child(b'{"partial json...', truncated=True)) is None


def test_parse_non_result_json_returns_none() -> None:
    assert parse_claude_result(_child(b'{"type": "stream_event", "data": 1}')) is None
