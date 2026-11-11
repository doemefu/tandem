"""Consult prompt composition and DECISION extraction."""

from __future__ import annotations

import pytest

from tandem.spec import (
    CONSULT_PERSONA,
    CONTEXT_CAP_BYTES,
    DECISION_INSTRUCTION,
    DELEGATE_CONTEXT_CAP_BYTES,
    QUESTION_CAP_BYTES,
    TASK_CAP_BYTES,
    compose_consult_prompt,
    compose_delegate_spec,
    extract_decision,
    parse_report_contract,
)


def test_compose_think_mode_has_persona_and_question() -> None:
    prompt = compose_consult_prompt("Solve X?", "some context", "think")
    assert CONSULT_PERSONA.split(". ")[0] in prompt  # persona leads
    assert DECISION_INSTRUCTION not in prompt
    assert "Context:\nsome context" in prompt
    assert "Question:\nSolve X?" in prompt


def test_compose_decide_mode_adds_decision_contract() -> None:
    prompt = compose_consult_prompt("Pick one", "", "decide")
    assert DECISION_INSTRUCTION in prompt
    assert "Question:\nPick one" in prompt


def test_compose_truncates_context_on_bytes() -> None:
    # 9 and 8 appear nowhere in the persona, so counting them isolates the caps.
    context = "9" * (CONTEXT_CAP_BYTES + 500)
    prompt = compose_consult_prompt("q", context, "think")
    assert prompt.count("9") == CONTEXT_CAP_BYTES


def test_compose_truncates_multibyte_context_on_byte_boundary() -> None:
    context = "é" * (CONTEXT_CAP_BYTES + 10)  # each é is 2 UTF-8 bytes
    prompt = compose_consult_prompt("q", context, "think")
    assert prompt.count("é") == CONTEXT_CAP_BYTES // 2
    assert len(prompt.encode("utf-8")) < CONTEXT_CAP_BYTES + 10_000  # roughly prompt-sized, not context-sized


def test_compose_rejects_oversized_question() -> None:
    with pytest.raises(ValueError, match="question exceeds"):
        compose_consult_prompt("8" * (QUESTION_CAP_BYTES + 1), "", "think")


def test_compose_rejects_multibyte_question_over_byte_cap() -> None:
    # 32KiB characters = 64KiB+ bytes: the cap is measured in bytes, not chars.
    with pytest.raises(ValueError, match="question exceeds"):
        compose_consult_prompt("é" * (QUESTION_CAP_BYTES // 2 + 1), "", "think")


def test_compose_rejects_empty_question() -> None:
    with pytest.raises(ValueError, match="question"):
        compose_consult_prompt("   ", "", "think")


def test_compose_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode"):
        compose_consult_prompt("q", "", "vibe")


def test_extract_decision_from_answer_with_trailer() -> None:
    answer = "1. Analysis\nIt is fine.\n\nDECISION: Use the cached probe.\n\nsession_id: b07e0c55-9f6a"
    assert extract_decision(answer) == "Use the cached probe."


def test_extract_decision_prefers_last_line() -> None:
    answer = "DECISION: first\nmore text\nDECISION: second"
    assert extract_decision(answer) == "second"


def test_extract_decision_missing_returns_none() -> None:
    assert extract_decision("1. Analysis\nNothing decisive here.") is None


def test_extract_decision_empty_value_returns_none() -> None:
    assert extract_decision("analysis\nDECISION:   ") is None


def test_compose_delegate_spec_sections() -> None:
    spec = compose_delegate_spec("Add the retry wrapper", "repo layout: src/")
    assert "## Goal\nAdd the retry wrapper" in spec
    assert "## Context\nrepo layout: src/" in spec
    assert "REPORT_STATUS: done | partial | blocked | failed" in spec
    assert "COMMANDS_RUN:" in spec


def test_compose_delegate_spec_rejects_empty_and_oversized_task() -> None:
    with pytest.raises(ValueError, match="task"):
        compose_delegate_spec("   ", "ctx")
    with pytest.raises(ValueError, match="exceeds"):
        compose_delegate_spec("t" * (TASK_CAP_BYTES + 1), "ctx")


def test_compose_delegate_spec_truncates_context_only() -> None:
    # 9 appears nowhere in the delegate template; counting isolates the cap.
    ctx = "9" * (DELEGATE_CONTEXT_CAP_BYTES + 500)
    spec = compose_delegate_spec("t", ctx)
    assert spec.count("9") == DELEGATE_CONTEXT_CAP_BYTES


# ---------------------------------------------------------------------------
# REVIEW round 2: the six-line REPORT contract parser
# ---------------------------------------------------------------------------

_GOOD_BLOCK = """Work done.

REPORT_STATUS: done
REPORT_SUMMARY: Fixed the wrapper and added tests.
FILES_CHANGED: src/a.py, tests/test_a.py
COMMANDS_RUN: pytest -q
RESULT_PATHS: src/a.py
REMAINING: none"""


def test_parse_report_contract_full_block() -> None:
    parsed = parse_report_contract(_GOOD_BLOCK)
    assert parsed is not None
    assert parsed["status"] == "done"
    assert parsed["summary"] == "Fixed the wrapper and added tests."
    assert parsed["files_changed"] == "src/a.py, tests/test_a.py"
    assert parsed["commands_run"] == "pytest -q"
    assert parsed["result_paths"] == "src/a.py"
    assert parsed["remaining"] == "none"


def test_parse_report_contract_missing_line_is_none() -> None:
    truncated = "\n".join(_GOOD_BLOCK.splitlines()[:-1])  # REMAINING gone
    assert parse_report_contract(truncated) is None


def test_parse_report_contract_last_occurrence_wins() -> None:
    # A doubled block: the child re-stated DONE after a self-correction.
    doubled = (
        _GOOD_BLOCK + "\n\nOne correction.\n\n" + "\n".join(_GOOD_BLOCK.splitlines()[-6:]) + "\nREPORT_STATUS: done"
    )
    parsed = parse_report_contract(doubled)
    assert parsed is not None
    assert parse_report_contract(doubled) == parse_report_contract(_GOOD_BLOCK)


def test_parse_report_contract_window_bounds_chatty_answers() -> None:
    # The contract lines sit far outside the tail window: no block counts.
    chatty = "prose line\n" * 50 + _GOOD_BLOCK + "\n" + "trailing prose\n" * 50
    assert parse_report_contract(chatty) is None


def test_parse_report_contract_empty_is_none() -> None:
    assert parse_report_contract("") is None
    assert parse_report_contract("no contract at all, just prose") is None
