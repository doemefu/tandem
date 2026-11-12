"""Consult prompts: the persona Claude is given for every tandem consult.

The persona pins output structure and forbids tool use, because a tandem
consult runs with the zero-tool flags (--tools "" and a zero-MCP config)
and must stay a text-in, text-out second opinion.  The context cap
bounds what a caller may paste in one payload.
"""

from __future__ import annotations

CONSULT_PERSONA = """You are Claude, consulted by another coding agent. You have no file, repo \
or web access in this session - reason only from the context given below. Reply in compact English \
prose with exactly these sections:
1. Analysis (at most 10 sentences).
2. Options: 2-4, one line each with the key tradeoff.
3. Recommendation: exactly ONE concrete option.
4. Assumptions you are making.
5. Confidence: low, medium or high, plus why.
Never ask the caller to run tools or fetch things for you; decide with what is given."""

DECISION_INSTRUCTION = (
    "End your final message with exactly one line of the form "
    "'DECISION: <one unqualified sentence, at most 200 characters>' that matches your Recommendation."
)

# Context is truncated BYTES-first when oversized (supporting material);
# an oversized question is rejected outright - silently answering a
# truncated question yields confidently wrong decisions.
CONTEXT_CAP_BYTES = 64 * 1024
QUESTION_CAP_BYTES = 64 * 1024

CONSULT_MODES = frozenset(("think", "decide"))


def _clip_bytes(text: str, cap: int) -> str:
    raw = text.encode("utf-8")[:cap]
    return raw.decode("utf-8", errors="ignore")


def compose_consult_prompt(question: str, context: str, mode: str) -> str:
    """Build the full prompt sent to Claude for one consult."""
    if mode not in CONSULT_MODES:
        msg = f"mode must be one of {', '.join(sorted(CONSULT_MODES))}, got {mode!r}"
        raise ValueError(msg)
    if not question.strip():
        msg = "question must not be empty"
        raise ValueError(msg)
    if len(question.encode("utf-8")) > QUESTION_CAP_BYTES:
        msg = f"question exceeds {QUESTION_CAP_BYTES} bytes - split the consult"
        raise ValueError(msg)
    sections = [CONSULT_PERSONA]
    if mode == "decide":
        sections.append(DECISION_INSTRUCTION)
    if context:
        sections.append("Context:\n" + _clip_bytes(context, CONTEXT_CAP_BYTES))
    sections.append("Question:\n" + question)
    return "\n\n".join(sections)


def extract_decision(answer: str) -> str | None:
    """Return the last DECISION line in ``answer``, or None.

    The reversed scan skips blank lines so multi-line footers between the
    decision and the end of the answer cannot shadow it; if several
    DECISION lines exist, the final one is the committed decision.
    """
    for line in reversed(answer.splitlines()):
        stripped = line.strip()
        if stripped.startswith("DECISION:"):
            decision = stripped.removeprefix("DECISION:").strip()
            return decision or None
    return None
