"""Consult and delegate prompt templates.

The consult persona pins output structure and forbids tool use, because a
tandem consult runs with the zero-tool flags (--tools "" and a zero-MCP
config) and must stay a text-in, text-out second opinion.  The context
caps bound what a caller may paste in one payload.  The delegate template
turns a task into a spec with a reporting contract, so a delegation comes
back verifiable instead of a stream of consciousness.
"""

from __future__ import annotations

CONSULT_PERSONA = """You are Claude, consulted by another coding agent. You have no file, repo or \
web access in this session - reason only from the context given below. Reply in compact English \
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

DELEGATE_SPEC_TEMPLATE = """Implement the following task in this repository.

## Goal
{task}

## Context
{context}"""

DELEGATE_REPORT_CONTRACT = """## Reporting contract
When you are done (or stopped), end your final report message with exactly these six lines, \
each prefixed as written, no extra prose after them:
REPORT_STATUS: done | partial | blocked | failed
REPORT_SUMMARY: <one sentence>
FILES_CHANGED: <paths of files you changed, comma-separated, or none>
COMMANDS_RUN: <commands you ran that matter to the audit, comma-separated, or none>
RESULT_PATHS: <paths a reviewer should read to verify, comma-separated, or none>
REMAINING: <what is left to do, or none>"""

# The six contract lines as machine-readable structure: prefix -> payload key.
REPORT_LINE_PREFIXES = (
    ("REPORT_STATUS:", "status"),
    ("REPORT_SUMMARY:", "summary"),
    ("FILES_CHANGED:", "files_changed"),
    ("COMMANDS_RUN:", "commands_run"),
    ("RESULT_PATHS:", "result_paths"),
    ("REMAINING:", "remaining"),
)
REPORT_CONTRACT_REQUIRED = len(REPORT_LINE_PREFIXES)
# The vocabulary the contract fixates for REPORT_STATUS.
REPORT_STATUS_VALUES = frozenset(("done", "partial", "blocked", "failed"))
# The tail window the parser scans: the contract lines are last in the report
# by construction, so a small window both bounds the work and rejects blocks
# echoed from the middle of a chatty answer.
REPORT_PARSE_WINDOW_LINES = 24

# Delegations carry specs, not questions: the caps are larger than consult's.
TASK_CAP_BYTES = 256 * 1024
DELEGATE_CONTEXT_CAP_BYTES = 512 * 1024

DELEGATE_MODES = ("accept-edits", "smart-approve")


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


def compose_delegate_spec(task: str, context: str) -> str:
    """Build the spec file content for one vibe delegation."""
    if not task.strip():
        msg = "task must not be empty"
        raise ValueError(msg)
    if len(task.encode("utf-8")) > TASK_CAP_BYTES:
        msg = f"task exceeds {TASK_CAP_BYTES} bytes - split the delegation"
        raise ValueError(msg)
    context_block = _clip_bytes(context, DELEGATE_CONTEXT_CAP_BYTES) if context else "none"
    return "\n\n".join(
        (
            DELEGATE_SPEC_TEMPLATE.format(task=task, context=context_block),
            DELEGATE_REPORT_CONTRACT,
        )
    )


def parse_report_contract(report: str) -> dict[str, str] | None:
    """Extract the last six-line REPORT block from a child report, else None.

    Scans the report tail in reverse and keeps the LAST line per prefix, so
    a doubled contract still yields the child's final state.  Only a complete
    block counts - a partially emitted contract is noise, not evidence
    (workers surface it as a warning rather than inventing fields).  List
    values stay raw comma-joined text; the worker splits and bounds them.
    """
    found: dict[str, str] = {}
    for line in reversed(report.splitlines()[-REPORT_PARSE_WINDOW_LINES:]):
        stripped = line.strip()
        for prefix, key in REPORT_LINE_PREFIXES:
            if stripped.startswith(prefix) and key not in found:
                found[key] = stripped.removeprefix(prefix).strip()
    if len(found) != REPORT_CONTRACT_REQUIRED:
        return None
    return found


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
