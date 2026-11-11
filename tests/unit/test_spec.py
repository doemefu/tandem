"""Consult prompt composition and DECISION extraction."""

from __future__ import annotations

import pytest

from tandem.spec import (
    CONSULT_PERSONA,
    CONTEXT_CAP_BYTES,
    DECISION_INSTRUCTION,
    QUESTION_CAP_BYTES,
    compose_consult_prompt,
    extract_decision,
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
