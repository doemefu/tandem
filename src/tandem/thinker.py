"""Thinker role tools: consult_claude and consult_claude_followup.

One consult runs one isolated ``claude -p`` child: zero built-in tools, a
zero-MCP config, no settings sources (no hooks, no CLAUDE.md), prompt on
stdin, structured persona prompt, JSON on stdout.

Caps are enforced before the spawn.  The rolling-24h consult budget is
consumed atomically under the master flock (check and add inside one
lock hold).  The cross-instance child-slot pre-check is deliberately
best-effort - it sits right before the spawn, so simultaneous racers
briefly oversubscribe by the number of racers, which is bounded and
benign.  A consumed budget unit is never refunded, not on spawn failure
and not on the Anthropic quota wall: the unit stands for one attempted
run, which keeps the cap honest against retry storms.  Cap refusals are
logged as ``consult_refused`` events for cap tuning.

Cost reporting is per-call: ``total_cost_usd`` from Claude is cumulative
across a session, so the registry stores the cumulative figure and the
tool returns the delta for this call.  Two simultaneous followups on
one session id can each attribute the other's spend to themselves
(multi-instance edge, bounded by the session TTL) - accepted.

Followups compose with the decide-mode persona (DECISION line included):
the dominant follow-up use is converging on a decision, and a forced
structured line never hides the analysis.  This is tandem policy, not a
Claude constraint.  A followup result must carry its own session_id;
a silent fallback to the old id would fork the thread invisibly, so it
is classified ``bad_response`` instead.
Followups spawn in the consult's pinned cwd, stored in the registry: the
current claude CLI resolves ``--resume`` ids cwd-agnostically (verified
live on claude 2.1.322), so the pin is not what makes resume work - it
keeps the child environment identical across a thread and insures
followups against a future CLI that binds transcripts to the project
directory again.  The pinned cwd is re-validated against the allowlist on
every followup (narrowed roots still fail closed), and a resume id Claude
no longer knows - registry hit but "No conversation found" on the child's
stderr - classifies ``unknown_session`` like any other dead id.
"""

from __future__ import annotations

import functools
import json
import re
import uuid
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from tandem.child import ChildResult, run_child
from tandem.config import build_child_env
from tandem.errors import CapExceededError, TandemError
from tandem.guard import child_mode, refusal
from tandem.runlog import sha256_hex
from tandem.runner.claude_runner import ClaudeOutcome, build_consult_args, parse_claude_result
from tandem.runtime import Runtime
from tandem.server import ensure_cli_versions
from tandem.sessions import SessionRecord, get_session, upsert_session, valid_session_id
from tandem.spec import CONSULT_MODES, CONTEXT_CAP_BYTES, compose_consult_prompt, extract_decision
from tandem.state import file_lock

DEFAULT_CONSULT_MAX_SECONDS = 180.0
MIN_CONSULT_MAX_SECONDS = 10.0
MAX_CONSULT_MAX_SECONDS = 1800.0

UNKNOWN_SESSION_COPY = (
    "Session {session_id} is unknown or expired (24h TTL). Do not retry this id - "
    "start a new consult_claude and include the needed context again."
)

CONSULT_TOOL_DESCRIPTION = (
    "Ask Claude - a separate Anthropic model with no file or repo access - for a one-shot consult. "
    "Pass everything Claude needs as text: the question plus optional context (paste code or file excerpts inline). "
    "Choose mode 'decide' when you need a final DECISION line, 'think' for open analysis. "
    "Use for architecture, tradeoffs and second opinions - not for facts readable locally. "
    "Optional cwd must be inside this tandem instance's allowlisted roots; unset means the server's own working "
    "directory. max_seconds must be 10-1800 (default 180). "
    "The answer ends with a session_id line for consult_claude_followup."
)
FOLLOWUP_TOOL_DESCRIPTION = (
    "Continue an earlier consult_claude conversation by passing the session_id it returned. "
    "Claude remembers that conversation, so add only the new context or question delta. "
    "Followups always run in decide mode: the answer targets a final DECISION line "
    "(the decision field is set whenever Claude complies) - use them to converge. "
    "With no valid session_id, start a new consult_claude instead - never invent or guess an id. "
    "max_seconds must be 10-1800 (default 180)."
)

# Consults spawn children and consume budget: NOT read-only, but never repo-
# destructive on their own (any file work happens in the caller's session).
TOOL_ANNOTATIONS = ToolAnnotations(read_only_hint=False, destructive_hint=False)

# Only the canonical CLI login-wall phrases classify as auth trouble; a
# stray "login" in an otherwise unrelated error stays child_error.
_AUTH_PHRASES = ("not logged in", "login expired")


def _validate_max_seconds(max_seconds: float) -> None:
    if not MIN_CONSULT_MAX_SECONDS <= max_seconds <= MAX_CONSULT_MAX_SECONDS:
        msg = (
            f"max_seconds must be between {MIN_CONSULT_MAX_SECONDS:.0f} and "
            f"{MAX_CONSULT_MAX_SECONDS:.0f}s, got {max_seconds}"
        )
        raise TandemError(msg, error_kind="invalid_arguments")


def _resolve_cwd(rt: Runtime, cwd: str) -> Path:
    """Resolve and validate the consult working directory.

    An unset cwd means the server's own working directory - validated by the
    same allowlist, so narrowing TANDEM_ALLOWED_ROOTS fails closed for both
    the explicit and the defaulted path.
    """
    candidate = Path(cwd).expanduser() if cwd else rt.cfg.cwd
    if not rt.cfg.is_cwd_allowed(candidate):
        msg = (
            f"cwd {candidate} is outside the allowlisted roots for this tandem instance "
            "(set TANDEM_ALLOWED_ROOTS or pass an allowed cwd)"
        )
        raise TandemError(msg, error_kind="invalid_cwd")
    return candidate


def _consume_budget(rt: Runtime) -> int:
    """Atomically check the rolling-24h consult budget and consume one unit."""
    with file_lock(rt.paths.lock):
        used = rt.counter.count()
        if used >= rt.cfg.daily_consult_cap:
            msg = (
                f"rolling-24h consult budget exhausted ({rt.cfg.daily_consult_cap} calls). "
                "It refills continuously as old calls age out - retry later, not immediately."
            )
            raise CapExceededError(msg)
        return rt.counter.add()


def _stderr_head(stderr: bytes, limit: int = 400) -> str:
    """Decode only the interesting slice first; the child keeps the head 8MiB."""
    return stderr[-2 * limit :].decode("utf-8", errors="replace").strip()[-limit:]


def _unknown_session_error(session_id: str) -> TandemError:
    return TandemError(UNKNOWN_SESSION_COPY.format(session_id=session_id[:32]), error_kind="unknown_session")


def _classify_childless(outcome: ClaudeOutcome | None, child: ChildResult, *, is_resume: bool) -> TandemError:
    """Error for a child that produced no usable result object."""
    detail = f" claude output tail: {_stderr_head(child.stderr)}" if child.stderr else ""
    if outcome is None:
        if child.timed_out:
            msg = (
                "Claude did not finish within the time limit. Treat this as no-answer; "
                f"at most one retry is sensible.{detail}"
            )
            return TandemError(msg, error_kind="timeout")
        # claude resolves an unknown/expired resume id out-of-band: rc 1,
        # no result object on stdout, "No conversation found" on stderr
        # (pinned live on claude 2.1.322).  The followup contract classifies
        # that the same way as an unknown registry id: no retry, start fresh.
        # Scoped to resumes: the phrase is resume-specific, and a new consult
        # must not claim its fresh id is unknown.
        if is_resume:
            stderr_text = child.stderr.decode("utf-8", errors="replace").lower()
            if "no conversation found" in re.sub(r"\s+", " ", stderr_text):
                msg = (
                    "Claude no longer has this conversation (expired or lost on Claude's "
                    "side). Do not retry the session_id - start a new consult_claude "
                    f"and include the needed context again. Claude said: {stderr_text.strip()[-200:]}"
                )
                return TandemError(msg, error_kind="unknown_session")
        if not child.stdout.strip():
            msg = f"Claude produced no output at all (stdout empty).{detail}"
            return TandemError(msg, error_kind="empty_stdout")
        note = "; the output cap may have cut it - retry with a smaller question" if child.stdout_truncated else ""
        msg = f"Claude stdout was not the expected JSON ({len(child.stdout)} bytes){note}.{detail}"
        return TandemError(msg, error_kind="bad_json")
    raise AssertionError(outcome)  # pragma: no cover - caller guards the branch


async def _consult_once(
    rt: Runtime,
    *,
    question: str,
    context: str,
    mode: str,
    resume: SessionRecord | None,
    cwd: Path,
    max_seconds: float,
) -> dict[str, Any]:
    """Shared consult pipeline; ``resume=None`` starts a new session."""
    call_kind = "new" if resume is None else "followup"
    try:
        question.encode("utf-8")
        context.encode("utf-8")
        prompt = compose_consult_prompt(question, context, mode)
    except UnicodeEncodeError as exc:
        raise TandemError("question/context must be valid UTF-8 text", error_kind="invalid_arguments") from exc
    except ValueError as exc:
        raise TandemError(str(exc), error_kind="invalid_arguments") from exc

    # Caps: slot check is a cheap read, the budget consume is the atomic one.
    busy = len(rt.registry.persistent())
    if busy >= rt.cfg.max_children:
        rt.log.event("consult_refused", call_kind=call_kind, mode=mode, reason="child_slots", children_busy=busy)
        msg = (
            f"child slots busy: {busy} of max {rt.cfg.max_children} children running "
            "across tandem instances. Retry once a current child finishes."
        )
        raise CapExceededError(msg)
    try:
        await anyio.to_thread.run_sync(functools.partial(_consume_budget, rt))
    except CapExceededError as exc:
        rt.log.event(
            "consult_refused", call_kind=call_kind, mode=mode, reason="daily_budget", daily_cap=rt.cfg.daily_consult_cap
        )
        raise exc

    new_session_id = None if resume is not None else str(uuid.uuid4())
    argv = build_consult_args(
        rt.cfg, session_id=new_session_id, resume_id=resume.session_id if resume is not None else None
    )
    run_id = f"consult-{uuid.uuid4().hex[:12]}"
    prompt_bytes = prompt.encode("utf-8")
    context_bytes_len = len(context.encode("utf-8"))

    await ensure_cli_versions(rt)

    rt.log.event(
        "consult_started",
        run_id=run_id,
        call_kind=call_kind,
        mode=mode,
        prompt_sha256=sha256_hex(prompt_bytes),
        prompt_bytes=len(prompt_bytes),
        session_id=resume.session_id if resume is not None else new_session_id,
        context_bytes=context_bytes_len,
        context_truncated=context_bytes_len > CONTEXT_CAP_BYTES,
        cwd=str(cwd),
        claude_version=rt.versions.get("claude"),
        vibe_version=rt.versions.get("vibe"),
    )

    child: ChildResult | None = None
    try:
        child = await run_child(
            argv,
            env=build_child_env(),
            cwd=cwd,
            timeout_s=max_seconds,
            grace_s=rt.cfg.grace_seconds,
            stdout_cap_bytes=rt.cfg.stdout_cap_bytes,
            stderr_keep_bytes=rt.cfg.stderr_keep_bytes,
            stdin_data=prompt_bytes,
            registry=rt.registry,
            run_id=run_id,
        )
        outcome = await anyio.to_thread.run_sync(functools.partial(parse_claude_result, child))
        payload = await _finish_consult(rt, child=child, outcome=outcome, resume=resume, cwd=cwd)
    except TandemError as exc:
        rt.log.event(
            "consult_completed",
            run_id=run_id,
            error_kind=exc.error_kind,
            duration_s=round(child.duration_s, 3) if child is not None else None,
            returncode=child.returncode if child is not None else None,
            timed_out=child.timed_out if child is not None else None,
            stdout_bytes=len(child.stdout) if child is not None else 0,
        )
        raise
    except BaseException:
        # Host cancellation or an internal crash: the runlog must not keep a
        # dangling consult_started; run_child's shielded cleanup owns the kill.
        rt.log.event(
            "consult_abandoned",
            run_id=run_id,
            duration_s=round(child.duration_s, 3) if child is not None else None,
            returncode=child.returncode if child is not None else None,
        )
        raise
    rt.log.event(
        "consult_completed",
        run_id=run_id,
        duration_s=round(child.duration_s, 3),
        returncode=child.returncode,
        timed_out=child.timed_out,
        num_turns=payload["num_turns"],
        cost_usd=payload["cost_usd"],
        session_id=payload["session_id"],
        stdout_bytes=len(child.stdout),
    )
    return payload


async def _finish_consult(
    rt: Runtime,
    *,
    child: ChildResult,
    outcome: ClaudeOutcome | None,
    resume: SessionRecord | None,
    cwd: Path,
) -> dict[str, Any]:
    """Turn one finished consult child into a payload or a classified TandemError.

    Classification order: a parsed error result wins over the timeout flag
    (the result object only exists once Claude finished writing it), and a
    complete successful result is used even when the child was slow enough
    to trip the watchdog.
    """
    if outcome is None:
        raise _classify_childless(outcome, child, is_resume=resume is not None)

    if outcome.is_error:
        result_text = outcome.answer or ""
        lowered = result_text.lower()
        if outcome.api_error == "usage_limit_reached":
            msg = f"Anthropic usage limit reached - terminal for now. Claude says: {result_text[:300]}"
            raise TandemError(msg, error_kind="quota_terminal")
        if outcome.api_error_status == 401 or any(phrase in lowered for phrase in _AUTH_PHRASES):
            msg = f"Claude authentication is not usable: {result_text[:300] or outcome.api_error or 'no detail'}"
            raise TandemError(msg, error_kind="auth_expired")
        msg = f"Claude error: {outcome.api_error or 'unknown api error'}. {result_text[:300]}"
        raise TandemError(msg, error_kind="child_error")

    answer = outcome.answer or ""
    if not answer.strip():
        msg = "Claude returned an empty answer"
        raise TandemError(msg, error_kind="bad_response")

    # New consults and followups alike must carry their own session id: a
    # resumed id that silently "sticks" would fork the thread invisibly.
    session_id = outcome.session_id
    if not session_id or not valid_session_id(session_id):
        msg = "Claude result carried no usable session_id for continuation"
        raise TandemError(msg, error_kind="bad_response")

    cost_cumulative = outcome.cost_usd
    if resume is None:
        # First call in a session: the cumulative figure is this call's cost.
        cost_delta: float | None = cost_cumulative
    elif cost_cumulative is not None and resume.cumulative_cost_usd is not None:
        # Followup: report only this call's share of the cumulative figure.
        cost_delta = round(max(0.0, cost_cumulative - resume.cumulative_cost_usd), 6)
    else:
        # No derivable delta (Claude omitted cost on some call of the thread).
        cost_delta = None

    await upsert_session(rt.sessions, session_id, cumulative_cost_usd=cost_cumulative, cwd=str(cwd))

    # Extraction never fails the consult: the answer is usable with or without the line.
    decision = extract_decision(answer)
    trailer = f"\n\nsession_id: {session_id}"
    return {
        "answer": answer + trailer,
        "decision": decision,
        "session_id": session_id,
        "num_turns": outcome.num_turns,
        "cost_usd": cost_delta,
        "is_error": False,
    }


async def consult_impl(
    rt: Runtime,
    *,
    question: str,
    context: str = "",
    mode: str = "think",
    cwd: str = "",
    max_seconds: float = DEFAULT_CONSULT_MAX_SECONDS,
) -> dict[str, Any]:
    """Ask Claude a fresh consult (see the tool description)."""
    if child_mode():
        raise refusal("consult_claude")
    if mode not in CONSULT_MODES:
        msg = f"mode must be one of {', '.join(sorted(CONSULT_MODES))}, got {mode!r}"
        raise TandemError(msg, error_kind="invalid_arguments")
    _validate_max_seconds(max_seconds)
    cwd_path = await anyio.to_thread.run_sync(functools.partial(_resolve_cwd, rt, cwd))
    return await _consult_once(
        rt, question=question, context=context, mode=mode, resume=None, cwd=cwd_path, max_seconds=max_seconds
    )


async def consult_followup_impl(
    rt: Runtime,
    *,
    session_id: str,
    question: str,
    context: str = "",
    max_seconds: float = DEFAULT_CONSULT_MAX_SECONDS,
) -> dict[str, Any]:
    """Continue a stored consult session (see the tool description)."""
    if child_mode():
        raise refusal("consult_claude_followup")
    _validate_max_seconds(max_seconds)
    if not valid_session_id(session_id):
        raise _unknown_session_error(session_id)
    record = await get_session(rt.sessions, session_id)
    if record is None:
        # Unknown, expired or cwd-less legacy records - the registry treats
        # all three identically: the caller starts a fresh consult.
        raise _unknown_session_error(session_id)
    cwd_path = await anyio.to_thread.run_sync(functools.partial(_resolve_cwd, rt, record.cwd))
    return await _consult_once(
        rt,
        question=question,
        context=context,
        mode="decide",
        resume=record,
        cwd=cwd_path,
        max_seconds=max_seconds,
    )


def _tool_error(exc: TandemError) -> ToolError:
    return ToolError(json.dumps(exc.payload(), separators=(",", ":")))


def add_tools(server: MCPServer, rt: Runtime) -> None:
    """Register the thinker tools on ``server`` for this runtime."""

    @server.tool(
        name="consult_claude",
        title="Consult Claude",
        description=CONSULT_TOOL_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS,
    )
    async def consult_claude(
        question: str,
        context: str = "",
        mode: str = "think",
        cwd: str = "",
        max_seconds: float = DEFAULT_CONSULT_MAX_SECONDS,
    ) -> dict[str, Any]:
        try:
            return await consult_impl(
                rt, question=question, context=context, mode=mode, cwd=cwd, max_seconds=max_seconds
            )
        except TandemError as exc:
            raise _tool_error(exc) from exc

    @server.tool(
        name="consult_claude_followup",
        title="Consult Claude followup",
        description=FOLLOWUP_TOOL_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS,
    )
    async def consult_claude_followup(
        session_id: str,
        question: str,
        context: str = "",
        max_seconds: float = DEFAULT_CONSULT_MAX_SECONDS,
    ) -> dict[str, Any]:
        try:
            return await consult_followup_impl(
                rt, session_id=session_id, question=question, context=context, max_seconds=max_seconds
            )
        except TandemError as exc:
            raise _tool_error(exc) from exc
