"""Worker role tool: delegate_to_vibe.

One delegation runs one headless vibe child against an explicit,
allowlisted, non-world-writable working directory: the task becomes a
spec file (with a reporting contract) under tandem's private state dir,
vibe gets ``--output-dir`` under the same private run dir, and two
``--disabled-tools`` regexes keep the child from reaching any MCP
server or built-in connector - the loop guard's second ring.  Vibe's
own graceful time limit (the vibe-side ``--time-limit`` flag) is set to
the caller's budget; tandem's hard kill sits on top of it with at least
30s of headroom.  An optional ``--max-price`` cap (operator env knob)
bounds per-run model spend - delegations deliberately do NOT consume
the rolling-24h consult budget, which prices Claude asks.

Run-dir hygiene: each delegation stages its spec (0600), export.json
and vibe's journal copy under ``<state>/tasks/delegate-<uuid12>/``,
swept whole-tree by the TTL at delegation time AND at server startup.

Classification: ``finished`` is success; limit stop_reasons
(deadline/turn_limit/token_limit/price_limit/length) are honest
partials reported with ``status: partial`` and not treated as errors;
``terminated`` and the error-ish stop_reasons raise; an exit without a
parseable export.json is ``child_crash``.  A success-shaped export with
a nonzero exit code is not trusted: the child result wins.  A hard kill
that still left a parseable export keeps the export-wins semantics but
the payload carries ``timed_out: true`` and a synthetic warning so the
caller can distrust the report text.  An empty report falls back to the
export's own answer field before ever failing the run.  The six-line REPORT
contract is parsed from the full report before any capping: the structured
fields ride the payload, REPORT_STATUS is surfaced next to the export-derived
status (a disagreement is a warning, never a rewrite), and answers over
``TANDEM_DELEGATE_RESULT_CAP_BYTES`` (default 30k) spill whole to
``report.md`` in the run dir so nothing is lost while the caller's context
stops absorbing the tail.
"""

from __future__ import annotations

import functools
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from tandem.child import ChildResult, run_child
from tandem.config import build_child_env
from tandem.errors import CapExceededError, StateError, TandemError
from tandem.guard import child_mode, refusal
from tandem.runlog import sha256_hex
from tandem.runner.vibe_runner import (
    STOP_REASON_ERRORS,
    STOP_REASON_LIMITS,
    VibeOutcome,
    build_delegate_args,
    parse_export,
)
from tandem.runtime import Runtime
from tandem.spec import (
    DELEGATE_CONTEXT_CAP_BYTES,
    DELEGATE_MODES,
    REPORT_STATUS_VALUES,
    compose_delegate_spec,
    parse_report_contract,
)
from tandem.state import atomic_write_bytes, ensure_dir, sweep_ttl_dirs

DEFAULT_DELEGATE_MAX_SECONDS = 600.0
MIN_DELEGATE_MAX_SECONDS = 30.0
MAX_DELEGATE_MAX_SECONDS = 3600.0
# Post-deadline headroom for vibe's graceful wrap-and-export path: SIGTERM
# patience and expected shutdown duration are different things, so the
# graces never drop below half a minute even with a micro-grace config.
MIN_KILL_HEADROOM_S = 30.0
DEFAULT_DELEGATE_MODE = "smart-approve"
# Per-field bounds for surfaced report contract values (audit noise control).
_REPORT_LIST_ITEM_MAX = 200
_REPORT_LIST_MAX_ITEMS = 20

DELEGATE_TOOL_DESCRIPTION = (
    "Delegate a well-defined implementation task to Vibe - a separate coding agent that works in "
    "the given cwd (must be inside this tandem instance's allowlisted roots; never the home "
    "directory, never world-writable). Pass the task as the spec: goal, constraints, deliverables, "
    "and how to verify. Vibe edits files in cwd and answers with a structured report (status, "
    "files changed, commands run, verification paths). Choose mode 'smart-approve' "
    "(default: file edits approved, and vibe's safety classifier auto-runs the shell commands it "
    "judges safe while commands needing a human are still denied - a headless run has no one to "
    "ask) or 'accept-edits' (file edits approved, but EVERY shell command is denied: headless "
    "mode cannot prompt a human for the approval those commands need - use it for edit-only "
    "tasks). The run trusts the repo: its .vibe config and hooks load and execute (operator knob "
    "TANDEM_DELEGATE_TRUST, default on). max_seconds must be 30-3600 (default 600). One "
    "delegation per call; the answer includes vibe's stop_reason so partial work is visible."
)
DELEGATE_ANNOTATIONS = ToolAnnotations(read_only_hint=False, destructive_hint=True)

_TRUNCATED_WARNING = "vibe was hard-killed at tandem's time budget; the report may be truncated"
_REPORT_MISSING_WARNING = "vibe wrote no report text; inspect export.json and the workdir"
_REPORT_CONTRACT_MISSING_WARNING = "report did not end with the six-line REPORT contract lines"
_STDOUT_CAPPED_WARNING = (
    "vibe stdout was capped by tandem's spooler; the report below is the export.json copy "
    "(complete where the export is)"
)


def _validate_max_seconds(max_seconds: float) -> None:
    if not MIN_DELEGATE_MAX_SECONDS <= max_seconds <= MAX_DELEGATE_MAX_SECONDS:
        msg = (
            f"max_seconds must be between {MIN_DELEGATE_MAX_SECONDS:.0f} and "
            f"{MAX_DELEGATE_MAX_SECONDS:.0f}s, got {max_seconds}"
        )
        raise TandemError(msg, error_kind="invalid_arguments")


def _resolve_workdir(rt: Runtime, cwd: str) -> Path:
    """The delegation target: required, allowlisted, and not world-writable."""
    if not cwd:
        msg = "cwd is required for a delegation (the directory vibe may edit)"
        raise TandemError(msg, error_kind="invalid_cwd")
    candidate = Path(cwd).expanduser()
    if not rt.cfg.is_cwd_allowed(candidate):
        msg = f"cwd {cwd} is outside the allowlisted roots for this tandem instance"
        raise TandemError(msg, error_kind="invalid_cwd")
    if stat.S_IWOTH & os.stat(candidate).st_mode:
        msg = (
            f"cwd {cwd} is world-writable - refusing to delegate: repo-local vibe "
            "config/hooks would execute with write access to everyone"
        )
        raise TandemError(msg, error_kind="invalid_cwd")
    return candidate


def _stderr_tail(stderr: bytes, limit: int = 400) -> str:
    """Decode only the interesting slice first: child stderr keeps the head 8MiB."""
    return stderr[-2 * limit :].decode("utf-8", errors="replace").strip()[-limit:]


def _clip_utf8(text: str, cap_bytes: int) -> str:
    return text.encode("utf-8")[:cap_bytes].decode("utf-8", errors="ignore")


def _split_list(value: str) -> list[str]:
    """Split a contract list value; the ``none`` sentinel and dupes vanish."""
    items = [item.strip() for item in value.split(",")]
    return [item[:_REPORT_LIST_ITEM_MAX] for item in items if item and item.lower() != "none"][:_REPORT_LIST_MAX_ITEMS]


def _text_or_none(value: str) -> str | None:
    """Contract scalar with the ``none`` sentinel normalized away."""
    if not value or value.lower() == "none":
        return None
    return value[: _REPORT_LIST_ITEM_MAX * 2]


def _contract_disagrees(status: str, report_status: str) -> bool:
    return (status == "completed" and report_status in {"partial", "blocked", "failed"}) or (
        status == "partial" and report_status == "done"
    )


async def delegate_impl(
    rt: Runtime,
    *,
    task: str,
    cwd: str,
    context: str = "",
    mode: str = DEFAULT_DELEGATE_MODE,
    max_seconds: float = DEFAULT_DELEGATE_MAX_SECONDS,
) -> dict[str, Any]:
    """Run one vibe delegation (see the tool description)."""
    if child_mode():
        raise refusal("delegate_to_vibe")
    if mode not in DELEGATE_MODES:
        msg = f"mode must be one of {', '.join(DELEGATE_MODES)}, got {mode!r}"
        raise TandemError(msg, error_kind="invalid_arguments")
    _validate_max_seconds(max_seconds)
    try:
        task.encode("utf-8")
        context.encode("utf-8")
        spec = compose_delegate_spec(task, context)
    except UnicodeEncodeError as exc:
        raise TandemError("task/context must be valid UTF-8 text", error_kind="invalid_arguments") from exc
    except ValueError as exc:
        raise TandemError(str(exc), error_kind="invalid_arguments") from exc

    workdir = await anyio.to_thread.run_sync(functools.partial(_resolve_workdir, rt, cwd))

    busy = len(rt.registry.persistent())
    if busy >= rt.cfg.max_children:
        rt.log.event("delegate_refused", reason="child_slots", children_busy=busy, mode=mode)
        msg = (
            f"child slots busy: {busy} of max {rt.cfg.max_children} children running "
            "across tandem instances. Retry once a current child finishes."
        )
        raise CapExceededError(msg)

    run_id = f"delegate-{uuid.uuid4().hex[:12]}"
    run_dir = rt.paths.tasks_dir / run_id
    spec_path = run_dir / "prompt.md"
    export_path = run_dir / "export.json"

    spec_bytes = spec.encode("utf-8")
    context_bytes_len = len(context.encode("utf-8"))

    def stage_run_dir() -> None:
        try:
            ensure_dir(run_dir)
            atomic_write_bytes(spec_path, spec_bytes)  # 0600 by design
            sweep_ttl_dirs(rt.paths.tasks_dir, rt.cfg.task_ttl_hours)
        except OSError as exc:
            raise StateError(f"cannot stage delegation under {rt.paths.tasks_dir}: {exc}") from exc

    try:
        await anyio.to_thread.run_sync(stage_run_dir)
    except StateError:
        rt.log.event("delegate_refused", reason="state_dir", mode=mode)
        raise

    argv = build_delegate_args(
        rt.cfg,
        workdir=workdir,
        prompt_file=spec_path,
        output_dir=run_dir,
        max_seconds=max_seconds,
        agent=mode,
        max_price_usd=rt.cfg.delegate_max_price,
    )
    rt.log.event(
        "delegate_started",
        run_id=run_id,
        mode=mode,
        workdir=str(workdir),
        spec_sha256=sha256_hex(spec_bytes),
        spec_bytes=len(spec_bytes),
        context_bytes=context_bytes_len,
        context_truncated=context_bytes_len > DELEGATE_CONTEXT_CAP_BYTES,
        max_seconds=max_seconds,
        trust=rt.cfg.delegate_trust,
        max_price_usd=rt.cfg.delegate_max_price,
        result_cap_bytes=rt.cfg.delegate_result_cap_bytes,
    )

    child: ChildResult | None = None
    try:
        child = await run_child(
            argv,
            env=build_child_env(),
            cwd=workdir,
            timeout_s=max_seconds + max(MIN_KILL_HEADROOM_S, rt.cfg.grace_seconds * 2),
            grace_s=rt.cfg.grace_seconds,
            stdout_cap_bytes=rt.cfg.stdout_cap_bytes,
            stderr_keep_bytes=rt.cfg.stderr_keep_bytes,
            stdin_data=None,
            registry=rt.registry,
            run_id=run_id,
        )
        outcome = await anyio.to_thread.run_sync(functools.partial(parse_export, export_path))
        payload = await _finish_delegation(
            rt, child=child, outcome=outcome, run_id=run_id, run_dir=run_dir, workdir=workdir
        )
    except TandemError as exc:
        rt.log.event(
            "delegate_completed",
            run_id=run_id,
            error_kind=exc.error_kind,
            duration_s=round(child.duration_s, 3) if child is not None else None,
            returncode=child.returncode if child is not None else None,
            timed_out=child.timed_out if child is not None else None,
        )
        raise
    except BaseException:
        rt.log.event(
            "delegate_abandoned",
            run_id=run_id,
            duration_s=round(child.duration_s, 3) if child is not None else None,
            returncode=child.returncode if child is not None else None,
        )
        raise
    rt.log.event(
        "delegate_completed",
        run_id=run_id,
        duration_s=round(child.duration_s, 3),
        returncode=child.returncode,
        timed_out=child.timed_out,
        mode=mode,
        trust=rt.cfg.delegate_trust,
        status=payload["status"],
        stop_reason=payload["stop_reason"],
        report_status=payload["report_status"],
        commands_run=payload["commands_run"],
        cost_usd=payload["cost_usd"],
        session_id=payload["session_id"],
        stdout_bytes=len(child.stdout),
    )
    return payload


async def _finish_delegation(
    rt: Runtime,
    *,
    child: ChildResult,
    outcome: VibeOutcome | None,
    run_id: str,
    run_dir: Path,
    workdir: Path,
) -> dict[str, Any]:
    """Turn one finished vibe child into a payload or a classified TandemError.

    The child's six-line REPORT contract is parsed from the full report
    BEFORE any result capping, and its ``REPORT_STATUS`` is surfaced next to
    the export-derived status; a disagreement between the two is a warning,
    never a rewrite of either side (the caller sees both and judges).
    Answers over ``delegate_result_cap_bytes`` spill whole into
    ``report.md`` under the run dir - nothing is lost, the caller's context
    just stops absorbing the tail.
    """
    report = child.stdout.decode("utf-8", errors="replace").strip()

    if outcome is None:
        evidence = f" (run dir: {run_dir})"
        if child.timed_out:
            msg = (
                "Vibe hit the hard time limit and was killed; it may have made partial "
                f"edits in {workdir} - inspect git status and git diff before retrying or "
                f"building on it.{evidence}"
            )
            raise TandemError(msg, error_kind="timeout")
        detail = f" vibe stderr tail: {_stderr_tail(child.stderr)}" if child.stderr else ""
        msg = f"Vibe exited without a parseable export.json (exit {child.returncode}).{detail}{evidence}"
        raise TandemError(msg, error_kind="child_crash")

    stop = outcome.stop_reason

    if stop in STOP_REASON_ERRORS or stop == "terminated":
        detail = _stderr_tail(child.stderr) if child.stderr else report[-300:]
        kind = "terminated" if stop == "terminated" else "child_error"
        msg = f"Vibe run ended with stop_reason {stop}. {(detail or 'no detail')[:300]} (run dir: {run_dir})"
        raise TandemError(msg, error_kind=kind)

    status = "partial" if stop in STOP_REASON_LIMITS else "completed"
    if status == "completed" and child.returncode not in (0, None):
        # A success-shaped export with a failing exit code is lies; trust the child.
        detail = _stderr_tail(child.stderr) if child.stderr else report[-300:]
        msg = (
            f"Vibe reported stop_reason {stop or 'unknown'} but exited with {child.returncode}. "
            f"{(detail or 'no detail')[:300]} (run dir: {run_dir})"
        )
        raise TandemError(msg, error_kind="child_error")

    warnings = list(outcome.warnings)
    # An unrecognized future stop_reason stays a success only loudly: surface the drift.
    known = stop is None or stop == "finished" or stop in STOP_REASON_LIMITS
    if not known:
        warnings.append(f"unrecognized stop_reason from vibe: {stop}")
    if child.timed_out:
        warnings.append(_TRUNCATED_WARNING)

    if not report:
        report = (outcome.answer or "").strip()
    elif child.stdout_truncated:
        # tandem cut the stdout head; the six contract lines live at the tail.
        # The export's own answer copy is complete, so it wins over the
        # truncated stdout - blaming the child for a missing contract tandem
        # itself cut would be dishonest.
        warnings.append(_STDOUT_CAPPED_WARNING)
        report = (outcome.answer or "").strip() or report

    block = parse_report_contract(report) if report else None
    report_status = (block or {}).get("status") or None
    if report and block is None:
        warnings.append(_REPORT_CONTRACT_MISSING_WARNING)
    if report_status is not None and report_status not in REPORT_STATUS_VALUES:
        warnings.append(f"unrecognized REPORT_STATUS value: {report_status}")
    if report_status is not None and _contract_disagrees(status, report_status):
        warnings.append(f"REPORT_STATUS {report_status} disagrees with export status {status}")

    if not report and status == "completed":
        msg = f"Vibe returned an empty report (run dir: {run_dir})"
        raise TandemError(msg, error_kind="bad_response")
    if not report and status == "partial":
        warnings.append(_REPORT_MISSING_WARNING)

    spill_path: Path | None = None
    cap = rt.cfg.delegate_result_cap_bytes
    if report and len(report.encode("utf-8")) > cap:
        report_bytes = report.encode("utf-8")
        spill_path = run_dir / "report.md"
        try:
            await anyio.to_thread.run_sync(functools.partial(atomic_write_bytes, spill_path, report_bytes))
            spill_complete = not child.stdout_truncated
            spill_note = (
                f"the full report is spilled to {spill_path}"
                if spill_complete
                else f"the report is spilled to {spill_path} (stdout was cap-truncated tandem-side, "
                "so the spill may lack the tail)"
            )
        except (OSError, StateError):
            # A finished run must not be lost to a spill write failure (disk
            # full, permission drift): degrade to clip-without-spill.
            spill_path = None
            spill_note = (
                f"answer exceeded the {cap}-byte result cap and the spill write failed; "
                "the visible answer is clipped with no spill copy (inspect the run dir)"
            )
        report = _clip_utf8(report, cap)
        warnings.append(f"answer exceeded the {cap}-byte result cap; {spill_note}")

    return {
        "answer": report,
        "status": status,
        "stop_reason": stop,
        "session_id": outcome.session_id,
        "cost_usd": outcome.cost_usd,
        "warnings": warnings,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "timed_out": child.timed_out,
        "report_truncated": child.stdout_truncated,
        "report_status": report_status,
        "report_summary": _text_or_none((block or {}).get("summary", "")),
        "files_changed": _split_list((block or {}).get("files_changed", "")),
        "commands_run": _split_list((block or {}).get("commands_run", "")),
        "result_paths": _split_list((block or {}).get("result_paths", "")),
        "remaining": _text_or_none((block or {}).get("remaining", "")),
        "spill_path": str(spill_path) if spill_path is not None else None,
        "is_error": False,
    }


def _tool_error(exc: TandemError) -> ToolError:
    return ToolError(json.dumps(exc.payload(), separators=(",", ":")))


def add_tools(server: MCPServer, rt: Runtime) -> None:
    """Register the worker tools on ``server`` for this runtime."""

    @server.tool(
        name="delegate_to_vibe",
        title="Delegate to Vibe",
        description=DELEGATE_TOOL_DESCRIPTION,
        annotations=DELEGATE_ANNOTATIONS,
    )
    async def delegate_to_vibe(
        task: str,
        cwd: str,
        context: str = "",
        mode: str = DEFAULT_DELEGATE_MODE,
        max_seconds: float = DEFAULT_DELEGATE_MAX_SECONDS,
    ) -> dict[str, Any]:
        try:
            return await delegate_impl(rt, task=task, cwd=cwd, context=context, mode=mode, max_seconds=max_seconds)
        except TandemError as exc:
            raise _tool_error(exc) from exc
