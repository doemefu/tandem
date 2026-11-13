"""Thinker orchestration with the Claude child fully faked.

Pure unit tests of the consult pipeline: guard first, caps before spawn,
argv wiring (--session-id vs --resume), cost delta math against the
registry's cumulative figure, failure classification, session registry
side effects and the runlog trail.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import tandem.thinker as thinker
from tandem.child import ChildRegistry, ChildResult, RegistryEntry
from tandem.config import Config
from tandem.errors import CapExceededError, ChildSpawnError, GuardRefusedError, TandemError
from tandem.runlog import RunLog
from tandem.runtime import Runtime
from tandem.sessions import SessionRegistry
from tandem.spec import QUESTION_CAP_BYTES
from tandem.state import RollingCounter, StatePaths, save_json

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SID = "b07e0c55-9f6a-4f9e-8c8a-1a2b3c4d5e6f"
DECISION_TEXT = "Ship Option B with first-use live probes and cache the results."


def _runtime(
    tmp_path: Path,
    *,
    role: str = "thinker",
    cap: int = 3,
    children_max: int = 2,
    allowed_root: Path | None = None,
) -> Runtime:
    root = allowed_root if allowed_root is not None else tmp_path
    cfg = Config.from_env(
        {
            "TANDEM_ROLE": role,
            "TANDEM_LOG_DIR": str(tmp_path / "state"),
            "TANDEM_ALLOWED_ROOTS": str(root),
            "TANDEM_DAILY_CONSULT_CAP": str(cap),
            "TANDEM_MAX_CHILDREN": str(children_max),
        }
    )
    # The server's own cwd defaults into consults and must be allowlisted in
    # tests (fail-closed policy; production relies on the operator's roots).
    cfg = replace(cfg, cwd=tmp_path, allowed_roots=(root,))
    paths = StatePaths(cfg.state_dir)
    paths.ensure_layout()
    return Runtime(
        cfg=cfg,
        paths=paths,
        registry=ChildRegistry(paths),
        counter=RollingCounter(paths.counters_file),
        sessions=SessionRegistry(paths),
        log=RunLog(paths.runs_dir, role=role),
        versions={"claude": "2.1.321", "vibe": "3.1.1"},  # pre-seeded: no real CLI probes in tests
    )


def _child_result(
    stdout: bytes = b"",
    *,
    timed_out: bool = False,
    stderr: bytes = b"",
    returncode: int | None = 0,
    truncated: bool = False,
) -> ChildResult:
    return ChildResult(
        returncode=returncode,
        signal=None,
        timed_out=timed_out,
        stdout=stdout,
        stdout_truncated=truncated,
        stderr=stderr,
        stderr_truncated=False,
        duration_s=0.5,
        first_byte_s=0.1,
    )


def _success_stdout(cost: float = 0.1245, session_id: str = SID) -> bytes:
    # cost/SID defaults are injected test data (deliberately not the live
    # fixture's values); the fixture pins the result-object shape only.
    payload: dict[str, Any] = json.loads((FIXTURES / "claude_print_success.json").read_text())
    payload["total_cost_usd"] = cost
    payload["session_id"] = session_id
    # The fixture pins the result-object shape (a live capture); the answer
    # text and turn count are test data - a decide-mode answer with the
    # DECISION line the persona asks for.
    payload["num_turns"] = 2
    payload["result"] = f"1. Analysis: wall and happy paths covered by live probes.\n\nDECISION: {DECISION_TEXT}"
    return json.dumps(payload).encode()


def _error_stdout(api_error: str | None, *, status: int | None = None, result: str = "boom") -> bytes:
    return json.dumps(
        {"type": "result", "is_error": True, "api_error": api_error, "api_error_status": status, "result": result}
    ).encode()


def _install_fake_child(monkeypatch: pytest.MonkeyPatch, result: ChildResult) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_run_child(argv: Any, **kwargs: Any) -> ChildResult:
        calls.append({"argv": [str(part) for part in argv], "kwargs": kwargs})
        return result

    monkeypatch.setattr(thinker, "run_child", fake_run_child)
    return calls


def _seed_busy(rt: Runtime, count: int) -> None:
    for index in range(count):
        rt.registry.register(
            RegistryEntry(
                run_id=f"live-{index}",
                pgid=12345 + index,
                started_at=time.time(),
                argv_hash="0" * 64,
                owner_pid=999999,
            )
        )


def _kind_of(exc: BaseException) -> str:
    assert isinstance(exc, TandemError)
    return exc.error_kind


# ---------------------------------------------------------------------------
# Guard, argument validation and cwd trust
# ---------------------------------------------------------------------------


def test_cli_versions_ride_the_first_started_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Version drift across a long-lived server (an auto-updated CLI) becomes a
    # runlog-visible fact: a spawned child's started event carries both CLI
    # versions from the once-per-process cache (pre-seeded here, so unit
    # tests never probe the real CLIs).
    rt = _runtime(tmp_path)
    _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    asyncio.run(thinker.consult_impl(rt, question="q"))
    events = [json.loads(line) for line in rt.log.path.read_text().splitlines()]
    started = events[0]
    assert started["claude_version"] == "2.1.321"
    assert started["vibe_version"] == "3.1.1"


def test_guard_refuses_new_consult(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    monkeypatch.setenv("TANDEM_CHILD", "1")
    with pytest.raises(GuardRefusedError):
        asyncio.run(thinker.consult_impl(rt, question="q"))
    assert calls == []


def test_guard_refuses_followup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    rt.sessions.upsert(SID, cumulative_cost_usd=0.05, cwd=str(tmp_path))
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    monkeypatch.setenv("TANDEM_CHILD", "1")
    with pytest.raises(GuardRefusedError):
        asyncio.run(thinker.consult_followup_impl(rt, session_id=SID, question="q"))
    assert calls == []


def test_invalid_mode_is_rejected_before_any_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q", mode="beg"))
    assert _kind_of(exc_info.value) == "invalid_arguments"
    assert calls == []


def test_empty_question_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="  "))
    assert _kind_of(exc_info.value) == "invalid_arguments"
    assert calls == []


def test_max_seconds_bounds_are_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    for bad in (1.0, 9999.0):
        with pytest.raises(TandemError) as exc_info:
            asyncio.run(thinker.consult_impl(rt, question="q", max_seconds=bad))
        assert _kind_of(exc_info.value) == "invalid_arguments"
    assert calls == []


def test_cwd_outside_allowlist_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q", cwd=str(tmp_path.parent)))
    assert _kind_of(exc_info.value) == "invalid_cwd"
    assert calls == []


# ---------------------------------------------------------------------------
# Caps: daily consult budget and cross-instance child slots
# ---------------------------------------------------------------------------


def test_daily_budget_exhausted_refuses_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path, cap=2)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    rt.counter.add()
    rt.counter.add()
    with pytest.raises(CapExceededError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q"))
    assert "budget" in str(exc_info.value)
    assert calls == []


def test_child_slots_busy_refuses_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path, children_max=2)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    _seed_busy(rt, 2)
    with pytest.raises(CapExceededError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q"))
    assert "child slots" in str(exc_info.value)
    assert calls == []


def test_slot_busy_only_counts_live_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path, children_max=2)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    _seed_busy(rt, 1)
    payload = asyncio.run(thinker.consult_impl(rt, question="q"))
    assert payload["is_error"] is False
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Happy path: new consult
# ---------------------------------------------------------------------------


def test_success_new_consult_full_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))

    payload = asyncio.run(thinker.consult_impl(rt, question="Pick a probe strategy", mode="decide"))

    assert payload["is_error"] is False
    assert payload["session_id"] == SID
    assert payload["answer"].endswith(f"session_id: {SID}")
    assert payload["decision"] == DECISION_TEXT
    assert payload["num_turns"] == 2
    assert payload["cost_usd"] == pytest.approx(0.1245)

    argv = calls[0]["argv"]
    assert argv[0] == "claude"
    assert argv[-2] == "--session-id"
    assert "--resume" not in argv
    assert len(argv[-1]) == 36  # a fresh uuid4, not the fixture's id

    record = rt.sessions.get(SID)
    assert record is not None
    assert record.consults == 1
    assert record.cumulative_cost_usd == pytest.approx(0.1245)
    assert rt.counter.count() == 1


def test_prompt_carries_persona_and_stdin_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    asyncio.run(thinker.consult_impl(rt, question="What about the startup cache?", context="ctx", mode="decide"))
    kwargs = calls[0]["kwargs"]
    prompt = kwargs["stdin_data"].decode()
    assert prompt.startswith("You are Claude")
    assert "DECISION:" in prompt
    assert "ctx" in prompt
    assert "What about the startup cache?" in prompt
    assert kwargs["cwd"] == tmp_path  # the validated server cwd default
    assert kwargs["timeout_s"] == 180.0


def test_runlog_trail_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    asyncio.run(thinker.consult_impl(rt, question="q"))
    lines = rt.log.path.read_text().splitlines()
    events = [json.loads(line) for line in lines]
    kinds = [event["kind"] for event in events]
    assert kinds == ["consult_started", "consult_completed"]
    completed = events[-1]
    assert "error_kind" not in completed
    assert completed["session_id"] == SID
    assert completed["cost_usd"] == pytest.approx(0.1245)


# ---------------------------------------------------------------------------
# Followups
# ---------------------------------------------------------------------------


def test_followup_resumes_and_reports_cost_delta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    rt.sessions.upsert(SID, cumulative_cost_usd=0.05, cwd=str(tmp_path))
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout(cost=0.12)))

    payload = asyncio.run(thinker.consult_followup_impl(rt, session_id=SID, question="Any changes?"))

    argv = calls[0]["argv"]
    assert argv[-2:] == ["--resume", SID]
    assert " --session-id" not in " ".join(argv)
    assert calls[0]["kwargs"]["cwd"] == tmp_path  # the consult's stored cwd
    assert payload["cost_usd"] == pytest.approx(0.07)
    assert payload["session_id"] == SID
    record = rt.sessions.get(SID)
    assert record is not None
    assert record.consults == 2
    assert record.cumulative_cost_usd == pytest.approx(0.12)
    assert record.cwd == str(tmp_path)
    # Followups compose with the decide persona.
    assert "DECISION:" in calls[0]["kwargs"]["stdin_data"].decode()


def test_followup_resumes_in_stored_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The followup must reuse the consult's pinned cwd, not the server's own
    # working directory - the child environment stays identical across a thread.
    project = tmp_path / "project"
    project.mkdir()
    rt = _runtime(tmp_path, allowed_root=tmp_path)
    rt.sessions.upsert(SID, cumulative_cost_usd=0.05, cwd=str(project))
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout(cost=0.12)))
    payload = asyncio.run(thinker.consult_followup_impl(rt, session_id=SID, question="q"))
    assert calls[0]["kwargs"]["cwd"] == project
    assert payload["is_error"] is False


def test_followup_stored_cwd_revalidates_against_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Roots narrowed between consult and followup: fail closed, no spawn.
    rt = _runtime(tmp_path, allowed_root=tmp_path)
    rt.sessions.upsert(SID, cumulative_cost_usd=0.05, cwd=str(tmp_path.parent))
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_followup_impl(rt, session_id=SID, question="q"))
    assert _kind_of(exc_info.value) == "invalid_cwd"
    assert calls == []


def test_followup_legacy_record_without_cwd_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A record predating the cwd pin cannot be replayed in the same child
    # environment - the caller starts a fresh consult.
    rt = _runtime(tmp_path)
    save_json(
        rt.paths.sessions_dir / f"{SID}.json",
        {
            "session_id": SID,
            "created": time.time(),
            "last_used": time.time(),
            "consults": 1,
            "cumulative_cost_usd": 0.05,
        },
    )
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_followup_impl(rt, session_id=SID, question="q"))
    assert _kind_of(exc_info.value) == "unknown_session"
    assert calls == []


def test_followup_without_prior_cost_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    rt.sessions.upsert(SID, cumulative_cost_usd=None, cwd=str(tmp_path))
    _install_fake_child(monkeypatch, _child_result(_success_stdout(cost=0.12)))
    payload = asyncio.run(thinker.consult_followup_impl(rt, session_id=SID, question="q"))
    assert payload["cost_usd"] is None  # first priced call in an unpriced session


def test_followup_unknown_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    ghost = "c0ffee00-1111-2222-3333-444444444444"
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_followup_impl(rt, session_id=ghost, question="q"))
    assert _kind_of(exc_info.value) == "unknown_session"
    assert "start a new consult_claude" in str(exc_info.value)
    assert calls == []


def test_followup_invalid_format_id_is_unknown_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_followup_impl(rt, session_id="short", question="q"))
    assert _kind_of(exc_info.value) == "unknown_session"
    assert calls == []


def test_followup_expired_session_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    rt.sessions.upsert(SID, cumulative_cost_usd=0.05, cwd=str(tmp_path))
    # Age the record past the 24h TTL, bypassing upsert's refresh.
    record_path = rt.paths.sessions_dir / f"{SID}.json"
    record = json.loads(record_path.read_text())
    record["last_used"] = time.time() - 25 * 3600
    record_path.write_text(json.dumps(record))
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_followup_impl(rt, session_id=SID, question="q"))
    assert _kind_of(exc_info.value) == "unknown_session"
    assert calls == []


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


def test_quota_wall_maps_to_quota_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    raw = (FIXTURES / "claude_print_usage_limit.json").read_bytes()
    _install_fake_child(monkeypatch, _child_result(raw))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q"))
    assert _kind_of(exc_info.value) == "quota_terminal"
    assert "usage limit" in str(exc_info.value).lower()
    assert rt.counter.count() == 1  # the wall consumed budget; policy: it was a real run


def test_auth_failure_maps_to_auth_expired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install_fake_child(monkeypatch, _child_result(_error_stdout(None, status=401, result="Please log in")))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q"))
    assert _kind_of(exc_info.value) == "auth_expired"


def test_other_api_error_maps_to_child_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install_fake_child(monkeypatch, _child_result(_error_stdout("overloaded_error", status=529)))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q"))
    assert _kind_of(exc_info.value) == "child_error"


def test_timeout_maps_to_timeout_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install_fake_child(monkeypatch, _child_result(b"", timed_out=True, returncode=None))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q", max_seconds=30))
    assert _kind_of(exc_info.value) == "timeout"


def test_empty_stdout_maps_to_empty_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install_fake_child(monkeypatch, _child_result(b""))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q"))
    assert _kind_of(exc_info.value) == "empty_stdout"


def test_non_json_stdout_maps_to_bad_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install_fake_child(monkeypatch, _child_result(b"plain text on stdout", stderr=b"warning noise"))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q"))
    kind = _kind_of(exc_info.value)
    assert kind == "bad_json"
    assert "output tail" in str(exc_info.value)


def test_result_without_session_id_is_bad_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    payload = json.loads(_success_stdout())
    payload["session_id"] = None
    _install_fake_child(monkeypatch, _child_result(json.dumps(payload).encode()))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q"))
    assert _kind_of(exc_info.value) == "bad_response"


def test_empty_answer_is_bad_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    payload = json.loads(_success_stdout())
    payload["result"] = "  "
    _install_fake_child(monkeypatch, _child_result(json.dumps(payload).encode()))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q"))
    assert _kind_of(exc_info.value) == "bad_response"


def test_failure_logs_error_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install_fake_child(monkeypatch, _child_result(b""))
    with pytest.raises(TandemError):
        asyncio.run(thinker.consult_impl(rt, question="q"))
    events = [json.loads(line) for line in rt.log.path.read_text().splitlines()]
    completed = events[-1]
    assert completed["kind"] == "consult_completed"
    assert completed["error_kind"] == "empty_stdout"


# ---------------------------------------------------------------------------
# Review fold: atomicity, spawn failures, robustness, classification order
# ---------------------------------------------------------------------------


def test_budget_consume_serializes_threads(tmp_path: Path) -> None:
    rt = _runtime(tmp_path, cap=2)
    refusals: list[str] = []

    def worker() -> None:
        try:
            thinker._consume_budget(rt)
        except CapExceededError:
            refusals.append("refused")

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert rt.counter.count() == 2  # exactly the cap; no lost updates, no overshoot
    assert len(refusals) == 8


def test_spawn_failure_logs_error_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    rt.counter.add()  # budget is consumed before the spawn attempt

    async def failing_run_child(argv: Any, **kwargs: Any) -> ChildResult:
        raise ChildSpawnError("failed to spawn 'claude'")

    monkeypatch.setattr(thinker, "run_child", failing_run_child)
    with pytest.raises(ChildSpawnError):
        asyncio.run(thinker.consult_impl(rt, question="q"))
    events = [json.loads(line) for line in rt.log.path.read_text().splitlines()]
    kinds = [event["kind"] for event in events]
    assert kinds == ["consult_started", "consult_completed"]
    assert events[-1]["error_kind"] == "spawn_failed"


def test_unencodable_question_rejected_before_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="broken \ud800 question"))
    assert _kind_of(exc_info.value) == "invalid_arguments"
    assert calls == []


def test_oversized_question_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q" * (QUESTION_CAP_BYTES + 1)))
    assert _kind_of(exc_info.value) == "invalid_arguments"
    assert calls == []


def test_context_truncation_surfaces_in_runlog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    asyncio.run(thinker.consult_impl(rt, question="q", context="x" * 200_000))
    events = [json.loads(line) for line in rt.log.path.read_text().splitlines()]
    started = events[0]
    assert started["kind"] == "consult_started"
    assert started["context_truncated"] is True
    assert started["context_bytes"] == 200_000


def test_error_result_beats_timeout_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install_fake_child(monkeypatch, _child_result(_error_stdout("overloaded_error"), timed_out=True, returncode=None))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q"))
    assert _kind_of(exc_info.value) == "child_error"  # not timeout: the result object won


def test_success_result_beats_timeout_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install_fake_child(monkeypatch, _child_result(_success_stdout(), timed_out=True))
    payload = asyncio.run(thinker.consult_impl(rt, question="q"))
    assert payload["is_error"] is False
    events = [json.loads(line) for line in rt.log.path.read_text().splitlines()]
    completed = events[-1]
    assert completed["kind"] == "consult_completed"
    assert "error_kind" not in completed
    assert completed["timed_out"] is True


def test_bad_json_after_capcut_mentions_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install_fake_child(monkeypatch, _child_result(b'{"partial": ', truncated=True))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q"))
    assert _kind_of(exc_info.value) == "bad_json"
    assert "cap may have cut" in str(exc_info.value)


def test_followup_missing_session_id_is_bad_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    rt.sessions.upsert(SID, cumulative_cost_usd=0.05, cwd=str(tmp_path))
    payload = json.loads(_success_stdout())
    payload["session_id"] = None  # a forked thread without a reported id
    _install_fake_child(monkeypatch, _child_result(json.dumps(payload).encode()))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_followup_impl(rt, session_id=SID, question="q"))
    assert _kind_of(exc_info.value) == "bad_response"


def test_runlog_stays_metadata_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    asyncio.run(thinker.consult_impl(rt, question="unique question marker 4f2a", context="unique ctx 9b1c"))
    runlog_text = rt.log.path.read_text()
    assert "unique question marker" not in runlog_text
    assert "Analysis" not in runlog_text  # answer content must stay out too


def test_cap_refusals_are_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path, cap=1)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    rt.counter.add()
    with pytest.raises(CapExceededError):
        asyncio.run(thinker.consult_impl(rt, question="q"))
    events = [json.loads(line) for line in rt.log.path.read_text().splitlines()]
    assert len(events) == 1  # refused before anything started
    refusal = events[0]
    assert refusal["kind"] == "consult_refused"
    assert refusal["reason"] == "daily_budget"
    assert refusal["call_kind"] == "new"
    assert refusal["daily_cap"] == 1
    assert calls == []

    other = _runtime(tmp_path, children_max=2)
    other.sessions.upsert(SID, cumulative_cost_usd=0.05, cwd=str(tmp_path))
    _seed_busy(other, 2)
    with pytest.MonkeyPatch.context() as mp:
        _install_fake_child(mp, _child_result(_success_stdout()))
        with pytest.raises(CapExceededError):
            asyncio.run(thinker.consult_followup_impl(other, session_id=SID, question="q"))
    events2 = [json.loads(line) for line in other.log.path.read_text().splitlines()]
    assert len(events2) == 1
    assert events2[0]["kind"] == "consult_refused"
    assert events2[0]["reason"] == "child_slots"
    assert events2[0]["call_kind"] == "followup"


def test_cwd_default_falls_back_to_validated_server_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    asyncio.run(thinker.consult_impl(rt, question="q"))
    assert calls[0]["kwargs"]["cwd"] == tmp_path


def test_cwd_default_fails_closed_when_server_cwd_untrusted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other_root = tmp_path / "other"
    other_root.mkdir()
    rt = _runtime(tmp_path, allowed_root=other_root)  # cfg.cwd=tmp_path is NOT allowed now
    calls = _install_fake_child(monkeypatch, _child_result(_success_stdout()))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q"))
    assert _kind_of(exc_info.value) == "invalid_cwd"
    assert calls == []


# ---------------------------------------------------------------------------
# Review fold, round 2: live followup fixture, unknown-conversation resume,
# auth-phrase exactness
# ---------------------------------------------------------------------------

LIVE_SID = "7f3d1a2c-9b4e-4c5d-8a6f-1e2b3c4d5e6f"
LIVE_FIRST_CALL_COST = 0.0182218


def test_live_followup_fixture_pins_cumulative_cost_delta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The fixture is a live capture of a resumed consultation (claude 2.1.322):
    # total_cost_usd is CUMULATIVE over the session, so the registry stores the
    # cumulative figure and the followup reports the delta to this call.
    rt = _runtime(tmp_path)
    rt.sessions.upsert(LIVE_SID, cumulative_cost_usd=LIVE_FIRST_CALL_COST, cwd=str(tmp_path))
    raw = (FIXTURES / "claude_print_followup.json").read_bytes()
    calls = _install_fake_child(monkeypatch, _child_result(raw))

    payload = asyncio.run(thinker.consult_followup_impl(rt, session_id=LIVE_SID, question="q"))

    assert calls[0]["argv"][-2:] == ["--resume", LIVE_SID]
    assert calls[0]["kwargs"]["cwd"] == tmp_path
    assert payload["session_id"] == LIVE_SID
    assert payload["cost_usd"] == pytest.approx(0.003175)  # pinned literal, not recomputed
    assert payload["cost_usd"] == pytest.approx(round(0.0213966 - LIVE_FIRST_CALL_COST, 6))
    record = rt.sessions.get(LIVE_SID)
    assert record is not None
    assert record.cumulative_cost_usd == pytest.approx(0.0213966)  # the registry keeps cumulative
    assert record.cwd == str(tmp_path)


def test_resume_unknown_conversation_is_unknown_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # claude refuses an unknown/expired resume id out-of-band: rc 1, no result
    # object on stdout, "No conversation found" on stderr (pinned live on
    # claude 2.1.322).  That classifies unknown_session - no retry, fresh consult.
    rt = _runtime(tmp_path)
    rt.sessions.upsert(SID, cumulative_cost_usd=0.05, cwd=str(tmp_path))
    stderr = f"No conversation found with session ID: {SID}".encode()
    calls = _install_fake_child(monkeypatch, _child_result(b"", stderr=stderr, returncode=1))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_followup_impl(rt, session_id=SID, question="q"))
    assert _kind_of(exc_info.value) == "unknown_session"
    assert "start a new consult_claude" in str(exc_info.value)
    assert len(calls) == 1  # the spawn happened; Claude refused the id


def test_unknown_conversation_phrase_scoped_to_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A NEW consult (no --resume) whose stderr merely contains the phrase must
    # not claim its fresh id is unknown - the classification is resume-scoped.
    rt = _runtime(tmp_path)
    calls = _install_fake_child(monkeypatch, _child_result(b"", stderr=b"no conversation found anywhere", returncode=1))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q"))
    assert _kind_of(exc_info.value) == "empty_stdout"
    assert len(calls) == 1


def test_stray_login_word_does_not_classify_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # "log in" inside an unrelated server error stays child_error - only the
    # canonical login-wall phrases classify as auth trouble.
    rt = _runtime(tmp_path)
    _install_fake_child(
        monkeypatch, _child_result(_error_stdout("server_error", status=500, result="users cannot log in right now"))
    )
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q"))
    assert _kind_of(exc_info.value) == "child_error"


def test_canonical_login_wall_phrase_is_auth_expired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install_fake_child(monkeypatch, _child_result(_error_stdout(None, result="Not logged in. Run /login.")))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(thinker.consult_impl(rt, question="q"))
    assert _kind_of(exc_info.value) == "auth_expired"
