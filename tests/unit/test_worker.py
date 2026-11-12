"""Worker orchestration with the vibe child fully faked.

Guard first, required+allowlisted cwd, cross-instance slot cap, spec file
staging (0600, TTL sweep), argv wiring, export.json classification
(finished / limit partials / terminated / error stop_reasons /
child_crash on missing export), runlog trail, ToolError mapping.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import tandem.worker as worker
from tandem.child import ChildRegistry, ChildResult, RegistryEntry
from tandem.config import Config
from tandem.errors import CapExceededError, GuardRefusedError, TandemError
from tandem.runlog import RunLog
from tandem.runtime import Runtime
from tandem.sessions import SessionRegistry
from tandem.spec import TASK_CAP_BYTES
from tandem.state import RollingCounter, StatePaths

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _runtime(
    tmp_path: Path,
    *,
    children_max: int = 2,
    delegate_trust: bool = True,
    max_price: str | None = None,
    result_cap: str | None = None,
) -> Runtime:
    env = {
        "TANDEM_ROLE": "worker",
        "TANDEM_LOG_DIR": str(tmp_path / "state"),
        "TANDEM_ALLOWED_ROOTS": str(tmp_path),
        "TANDEM_MAX_CHILDREN": str(children_max),
        "TANDEM_DELEGATE_TRUST": "true" if delegate_trust else "false",
        "TANDEM_DELEGATE_MAX_PRICE": max_price or "",
    }
    if result_cap:
        env["TANDEM_DELEGATE_RESULT_CAP_BYTES"] = result_cap
    cfg = Config.from_env(env)
    cfg = replace(cfg, cwd=tmp_path, allowed_roots=(tmp_path,))
    paths = StatePaths(cfg.state_dir)
    paths.ensure_layout()
    return Runtime(
        cfg=cfg,
        paths=paths,
        registry=ChildRegistry(paths),
        counter=RollingCounter(paths.counters_file),
        sessions=SessionRegistry(paths),
        log=RunLog(paths.runs_dir, role="worker"),
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


class FakeVibe:
    """run_child replacement: captures argv and writes a fixture export like vibe would."""

    def __init__(self, *, export: Path | None, child: ChildResult) -> None:
        self.calls: list[dict[str, Any]] = []
        self._export = export
        self._child = child

    async def __call__(self, argv: Any, **kwargs: Any) -> ChildResult:
        argv_list = [str(part) for part in argv]
        self.calls.append({"argv": argv_list, "kwargs": kwargs})
        if self._export is not None:
            out_dir = Path(argv_list[argv_list.index("--output-dir") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "export.json").write_text(self._export.read_text())
        return self._child


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    export: Path | None,
    child: ChildResult,
) -> FakeVibe:
    fake = FakeVibe(export=export, child=child)
    monkeypatch.setattr(worker, "run_child", fake)
    return fake


def _kind_of(exc: BaseException) -> str:
    assert isinstance(exc, TandemError)
    return exc.error_kind


def _export_bytes(fixture: str) -> Path:
    return FIXTURES / fixture


FUZZ_REPORT = (
    b"Done. Implemented the wrapper.\n\n"
    b"REPORT_STATUS: done\n"
    b"REPORT_SUMMARY: Implemented the retry wrapper and added tests.\n"
    b"FILES_CHANGED: src/wrapper.py, tests/test_wrapper.py\n"
    b"COMMANDS_RUN: pytest -q\n"
    b"RESULT_PATHS: src/wrapper.py\n"
    b"REMAINING: none"
)

# ---------------------------------------------------------------------------
# Guard, argument validation, cwd trust
# ---------------------------------------------------------------------------


def test_guard_refuses_delegation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(FUZZ_REPORT))
    monkeypatch.setenv("TANDEM_CHILD", "1")
    with pytest.raises(GuardRefusedError):
        asyncio.run(worker.delegate_impl(rt, task="do it", cwd=str(tmp_path)))
    assert fake.calls == []


def test_invalid_mode_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(b""))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path), mode="auto-approve"))
    assert _kind_of(exc_info.value) == "invalid_arguments"
    assert fake.calls == []


def test_max_seconds_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(b""))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path), max_seconds=5.0))
    assert _kind_of(exc_info.value) == "invalid_arguments"
    assert fake.calls == []


def test_cwd_is_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(b""))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(worker.delegate_impl(rt, task="t", cwd=""))
    assert _kind_of(exc_info.value) == "invalid_cwd"
    assert fake.calls == []


def test_cwd_outside_allowlist_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(b""))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path.parent)))
    assert _kind_of(exc_info.value) == "invalid_cwd"
    assert fake.calls == []


def test_home_cwd_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(b""))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(Path.home())))
    assert _kind_of(exc_info.value) == "invalid_cwd"
    assert fake.calls == []


def test_oversized_task_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(b""))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(worker.delegate_impl(rt, task="t" * (TASK_CAP_BYTES + 1), cwd=str(tmp_path)))
    assert _kind_of(exc_info.value) == "invalid_arguments"
    assert fake.calls == []


def test_unencodable_task_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(b""))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(worker.delegate_impl(rt, task="b\ud800d", cwd=str(tmp_path)))
    assert _kind_of(exc_info.value) == "invalid_arguments"
    assert fake.calls == []


def test_slot_cap_refuses_before_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path, children_max=1)
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(b""))
    rt.registry.register(
        RegistryEntry(run_id="live-x", pgid=4242, started_at=time.time(), argv_hash="0" * 64, owner_pid=999999)
    )
    with pytest.raises(CapExceededError):
        asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert fake.calls == []
    # No spec dir staged for a refused delegation.
    assert list(rt.paths.tasks_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_success_delegation_full_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    fake = FakeVibe(export=_export_bytes("vibe_export_success.json"), child=_child_result(FUZZ_REPORT))
    monkeypatch.setattr(worker, "run_child", fake)

    payload = asyncio.run(
        worker.delegate_impl(rt, task="Add the retry wrapper", cwd=str(tmp_path), context="repo layout: src/")
    )

    assert payload["is_error"] is False
    assert payload["status"] == "completed"
    assert payload["stop_reason"] == "finished"
    assert payload["answer"] == FUZZ_REPORT.decode()
    assert payload["session_id"] == "c47104a2-77e6-4bd0-a2a7-93f805e70d00"
    assert payload["warnings"] == []
    assert payload["timed_out"] is False
    assert payload["report_truncated"] is False
    # The six-line REPORT contract, parsed by the worker (default mode smart-approve).
    assert payload["report_status"] == "done"
    assert payload["report_summary"] == "Implemented the retry wrapper and added tests."
    assert payload["files_changed"] == ["src/wrapper.py", "tests/test_wrapper.py"]
    assert payload["commands_run"] == ["pytest -q"]
    assert payload["result_paths"] == ["src/wrapper.py"]
    assert payload["remaining"] is None
    assert payload["spill_path"] is None

    argv = fake.calls[0]["argv"]
    assert argv[0] == "vibe"
    assert argv[argv.index("--agent") + 1] == "smart-approve"
    assert "--trust" in argv  # delegate_trust default
    assert argv.count("--disabled-tools") == 2  # MCP + connectors off

    kwargs = fake.calls[0]["kwargs"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["registry"] is rt.registry
    assert kwargs["run_id"].startswith("delegate-")
    assert kwargs["run_id"] == payload["run_id"]
    # The hard kill wall must sit strictly after vibe's own graceful deadline;
    # fractional budgets floor the vibe-side flag but not the kill wall.
    assert kwargs["timeout_s"] == 600.0 + max(worker.MIN_KILL_HEADROOM_S, rt.cfg.grace_seconds * 2)
    assert kwargs["grace_s"] == rt.cfg.grace_seconds

    assert tmp_path.name in payload["run_dir"]
    run_dir = Path(payload["run_dir"])
    spec = (run_dir / "prompt.md").read_text()
    assert "Add the retry wrapper" in spec
    assert "REPORT_STATUS: done | partial | blocked | failed" in spec
    spec_mode = (run_dir / "prompt.md").stat().st_mode
    assert spec_mode & stat.S_IROTH == 0
    assert spec_mode & stat.S_IWOTH == 0
    assert json.loads((run_dir / "export.json").read_text())["stop_reason"] == "finished"


def test_spec_file_written_no_trust_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path, delegate_trust=False)
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(FUZZ_REPORT))
    asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert "--trust" not in fake.calls[0]["argv"]


def test_limit_stop_reason_is_partial_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install(monkeypatch, export=_export_bytes("vibe_export_deadline.json"), child=_child_result(FUZZ_REPORT))
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert payload["is_error"] is False
    assert payload["status"] == "partial"
    assert payload["stop_reason"] == "deadline"
    events = [json.loads(line) for line in rt.log.path.read_text().splitlines()]
    assert events[-1]["kind"] == "delegate_completed"
    assert events[-1]["status"] == "partial"


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


def test_missing_export_is_child_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install(monkeypatch, export=None, child=_child_result(b"", stderr=b"panic: bad flag"))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert _kind_of(exc_info.value) == "child_crash"
    assert "bad flag" in str(exc_info.value)


def test_timed_out_without_export_is_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install(monkeypatch, export=None, child=_child_result(b"", timed_out=True, returncode=None))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path), max_seconds=60))
    assert _kind_of(exc_info.value) == "timeout"


def test_terminated_stop_reason_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    export = tmp_path / "terminated-export.json"
    export.write_text(json.dumps({"stop_reason": "terminated", "session_id": "ab" * 8}))
    _install(monkeypatch, export=export, child=_child_result(b"job killed"))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert _kind_of(exc_info.value) == "terminated"


def test_error_stop_reasons_raise_child_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    for reason in ("usage_error", "config_error", "infrastructure_failure", "refusal", "aborted"):
        export = tmp_path / f"{reason}.json"
        export.write_text(json.dumps({"stop_reason": reason}))
        _install(monkeypatch, export=export, child=_child_result(b""))
        with pytest.raises(TandemError) as exc_info:
            asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
        assert _kind_of(exc_info.value) == "child_error"


def test_success_export_with_failing_exit_is_child_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(b"ok", returncode=3))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert _kind_of(exc_info.value) == "child_error"


def test_failure_logs_error_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install(monkeypatch, export=None, child=_child_result(b"", returncode=1, stderr=b"nope"))
    with pytest.raises(TandemError):
        asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    events = [json.loads(line) for line in rt.log.path.read_text().splitlines()]
    assert [event["kind"] for event in events] == ["delegate_started", "delegate_completed"]
    assert events[-1]["error_kind"] == "child_crash"


def test_spec_never_reaches_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(FUZZ_REPORT))
    asyncio.run(worker.delegate_impl(rt, task="secret-marker-8f3a", cwd=str(tmp_path)))
    joined = " ".join(fake.calls[0]["argv"])
    assert "secret-marker-8f3a" not in joined


# ---------------------------------------------------------------------------
# Review fold: hard-kill interplay, report recovery, run-dir hygiene
# ---------------------------------------------------------------------------


def test_deadline_export_plus_hard_kill_is_partial_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _runtime(tmp_path)
    _install(
        monkeypatch,
        export=_export_bytes("vibe_export_deadline.json"),
        child=_child_result(b"partial report", timed_out=True, returncode=None),
    )
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert payload["status"] == "partial"
    assert payload["timed_out"] is True
    assert any("hard-killed" in warning for warning in payload["warnings"])


def test_finished_export_survives_hard_kill_with_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install(
        monkeypatch,
        export=_export_bytes("vibe_export_success.json"),
        child=_child_result(b"done report", timed_out=True, returncode=None),
    )
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert payload["status"] == "completed"
    assert payload["timed_out"] is True
    assert any("hard-killed" in warning for warning in payload["warnings"])


def test_empty_stdout_falls_back_to_export_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(b""))
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert payload["answer"].startswith("Done. Implemented the retry wrapper")  # from export.json outcome.answer
    assert payload["status"] == "completed"


def test_completed_with_no_report_anywhere_is_bad_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    export = tmp_path / "finished-no-answer.json"
    export.write_text(json.dumps({"stop_reason": "finished", "session_id": None, "outcome": {}}))
    _install(monkeypatch, export=export, child=_child_result(b""))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert _kind_of(exc_info.value) == "bad_response"


def test_partial_with_no_report_carries_missing_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    export = tmp_path / "deadline-no-answer.json"
    export.write_text(json.dumps({"stop_reason": "deadline", "session_id": None}))
    _install(monkeypatch, export=export, child=_child_result(b""))
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert payload["status"] == "partial"
    assert any("no report text" in warning for warning in payload["warnings"])


def test_unknown_stop_reason_stays_success_but_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    export = tmp_path / "future.json"
    export.write_text(json.dumps({"stop_reason": "some_future_reason"}))
    _install(monkeypatch, export=export, child=_child_result(b"looks fine"))
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert payload["status"] == "completed"
    assert any("unrecognized stop_reason" in warning for warning in payload["warnings"])


def test_timeout_copy_warns_about_partial_edits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install(monkeypatch, export=None, child=_child_result(b"", timed_out=True, returncode=None))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path), max_seconds=60))
    message = str(exc_info.value)
    assert "may have made partial edits" in message
    assert str(tmp_path) in message  # the workdir to inspect
    assert "run dir" in message  # where the staged spec/evidence lives


def test_stale_run_dirs_are_swept_on_next_delegation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    stale = rt.paths.tasks_dir / "delegate-deadbeefdead"
    stale.mkdir(parents=True)
    (stale / "prompt.md").write_text("old")
    old = time.time() - 25 * 3600
    os.utime(stale, (old, old))
    _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(FUZZ_REPORT))
    asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert not stale.exists()
    assert list(rt.paths.tasks_dir.iterdir())  # the fresh run dir survives


def test_world_writable_cwd_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    floodable = tmp_path / "world"
    floodable.mkdir()
    floodable.chmod(0o777)
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(b""))
    try:
        with pytest.raises(TandemError) as exc_info:
            asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(floodable)))
        assert _kind_of(exc_info.value) == "invalid_cwd"
        assert "world-writable" in str(exc_info.value)
    finally:
        floodable.chmod(0o755)  # let pytest clean up
    assert fake.calls == []


def test_symlink_into_home_cwd_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    link = tmp_path / "home-link"
    link.symlink_to(Path.home())
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(b""))
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(link)))
    assert _kind_of(exc_info.value) == "invalid_cwd"
    assert fake.calls == []


def test_fractional_budget_floors_vibe_time_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(FUZZ_REPORT))
    asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path), max_seconds=90.9))
    argv = fake.calls[0]["argv"]
    idx = argv.index("--time-limit")
    assert argv[idx + 1] == "90"
    # the kill wall stays fractional-beyond-the-budget
    assert fake.calls[0]["kwargs"]["timeout_s"] == 90.9 + max(worker.MIN_KILL_HEADROOM_S, rt.cfg.grace_seconds * 2)


def test_max_price_cap_reaches_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path, max_price="0.75")
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(FUZZ_REPORT))
    asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    argv = fake.calls[0]["argv"]
    idx = argv.index("--max-price")
    assert argv[idx + 1] == "0.75"


def test_no_max_price_omits_the_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(FUZZ_REPORT))
    asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert "--max-price" not in fake.calls[0]["argv"]


def test_staging_failure_is_state_error_with_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(b""))

    def failing_stage(_path: Path) -> None:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(worker, "ensure_dir", failing_stage)
    with pytest.raises(TandemError) as exc_info:
        asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert _kind_of(exc_info.value) == "state"  # StateError: structured taxonomy, not a raw OSError
    events = [json.loads(line) for line in rt.log.path.read_text().splitlines()]
    assert events[-1]["kind"] == "delegate_refused"
    assert events[-1]["reason"] == "state_dir"


def test_export_warning_dicts_serialize_as_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Review finding: warnings came through as Python repr() of dicts - the
    # caller gets JSON strings with content, not `{'tool': 'bash'}` noise.
    rt = _runtime(tmp_path)
    export = tmp_path / "warn-export.json"
    export.write_text(
        json.dumps(
            {
                "stop_reason": "finished",
                "session_id": "ab" * 8,
                "warnings": [{"tool": "bash", "verdict": "auto-approved"}],
            }
        )
    )
    _install(monkeypatch, export=export, child=_child_result(FUZZ_REPORT))
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert payload["warnings"] == ['{"tool": "bash", "verdict": "auto-approved"}']


# ---------------------------------------------------------------------------
# Review round 2 (PR #3): REPORT contract surfacing, result cap + spill,
# runlog audit fields, default mode
# ---------------------------------------------------------------------------


def _block(status: str = "done", *, commands: str = "pytest -q") -> bytes:
    return (
        f"Work finished.\n\nREPORT_STATUS: {status}\n"
        f"REPORT_SUMMARY: Did the thing.\n"
        f"FILES_CHANGED: src/a.py\n"
        f"COMMANDS_RUN: {commands}\n"
        f"RESULT_PATHS: src/a.py\n"
        f"REMAINING: none"
    ).encode()


def test_report_status_disagreement_warns_not_rewrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Live finding: export says success while the child's own REPORT_STATUS
    # says partial - surface both plus a warning; neither side is rewritten.
    rt = _runtime(tmp_path)
    _install(
        monkeypatch,
        export=_export_bytes("vibe_export_success.json"),
        child=_child_result(_block(status="partial")),
    )
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert payload["status"] == "completed"
    assert payload["report_status"] == "partial"
    assert any("disagrees with export status" in warning for warning in payload["warnings"])


def test_missing_contract_block_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install(
        monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(b"Just prose, no contract.")
    )
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert payload["status"] == "completed"
    assert payload["report_status"] is None
    assert payload["commands_run"] == []
    assert any("six-line REPORT contract" in warning for warning in payload["warnings"])


def test_unrecognized_report_status_value_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install(
        monkeypatch,
        export=_export_bytes("vibe_export_success.json"),
        child=_child_result(_block(status="mostly-done")),
    )
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert payload["report_status"] == "mostly-done"  # surfaced verbatim for the caller to judge
    assert any("unrecognized REPORT_STATUS" in warning for warning in payload["warnings"])


def test_result_cap_spills_full_report_to_run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path, result_cap="1024")
    long_report = b"padding line\n" * 200 + _block()
    _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(long_report))
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert len(payload["answer"].encode()) == 1024  # visible answer clipped at the cap
    assert payload["spill_path"] is not None
    spill = Path(payload["spill_path"])
    assert spill.name == "report.md"
    assert spill.read_bytes() == long_report.strip()  # nothing lost
    assert spill.stat().st_mode & stat.S_IWOTH == 0
    assert any("result cap" in warning for warning in payload["warnings"])
    # The contract was parsed from the FULL report before capping.
    assert payload["report_status"] == "done"


def test_runlog_carries_audit_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path)
    _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(FUZZ_REPORT))
    asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    events = [json.loads(line) for line in rt.log.path.read_text().splitlines()]
    started, completed = events
    assert started["kind"] == "delegate_started"
    assert started["mode"] == "smart-approve"
    assert started["trust"] is True
    assert started["max_price_usd"] is None
    assert started["result_cap_bytes"] == 30_000
    assert completed["kind"] == "delegate_completed"
    assert completed["mode"] == "smart-approve"
    assert completed["trust"] is True
    assert completed["report_status"] == "done"
    assert completed["commands_run"] == ["pytest -q"]  # the child's self-reported audit


def test_explicit_mode_still_reaches_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # accept-edits stays available for edit-only tasks; only the DEFAULT moved.
    rt = _runtime(tmp_path)
    fake = _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(b""))
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path), mode="accept-edits"))
    argv = fake.calls[0]["argv"]
    assert argv[argv.index("--agent") + 1] == "accept-edits"
    assert payload["is_error"] is False


def test_truncated_stdout_yields_to_export_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The spooler keeps the stdout HEAD; the six contract lines live at the
    # tail.  When tandem capped stdout, the export's complete answer copy
    # wins before contract parsing - blaming the child for a contract tandem
    # itself cut would be dishonest.
    rt = _runtime(tmp_path)
    _install(
        monkeypatch,
        export=_export_bytes("vibe_export_success.json"),
        child=_child_result(b"truncated head...", truncated=True),
    )
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert payload["answer"].startswith("Done. Implemented the retry wrapper")  # export.outcome.answer
    assert any("capped by tandem" in warning for warning in payload["warnings"])


def test_deadline_with_nonzero_exit_is_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pinned live behavior (review round 2): limit-stop runs exit 3, and a
    # nonzero exit on a partial run is the norm, not an error.
    rt = _runtime(tmp_path)
    _install(
        monkeypatch,
        export=_export_bytes("vibe_export_deadline.json"),
        child=_child_result(_block(status="partial"), truncated=False, returncode=3),
    )
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert payload["status"] == "partial"
    assert payload["report_status"] == "partial"


def test_spill_write_failure_degrades_to_clip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A finished run must not be lost to a disk-full spill: no spill copy,
    # clipped answer, honest warning.
    rt = _runtime(tmp_path, result_cap="1024")
    long_report = b"padding line\n" * 200 + _block()
    _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(long_report))

    original_write = worker.atomic_write_bytes

    def failing_write(path: Path, data: bytes) -> None:
        if path.name == "report.md":  # the staging write must keep working
            raise OSError(28, "No space left on device")
        return original_write(path, data)

    monkeypatch.setattr(worker, "atomic_write_bytes", failing_write)
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert payload["spill_path"] is None
    assert len(payload["answer"].encode()) == 1024
    assert any("spill write failed" in warning for warning in payload["warnings"])
    # The audit fields still survived - they come from the pre-cap parse.
    assert payload["report_status"] == "done"


def test_report_fields_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A chatty child cannot smuggle bulk past the cap via the structured fields.
    rt = _runtime(tmp_path)
    chatty_block = (
        "Work.\n\nREPORT_STATUS: done\n"
        f"REPORT_SUMMARY: {'s' * 5000}\n"
        "FILES_CHANGED: src/a.py\n"
        "COMMANDS_RUN: pytest -q\n"
        "RESULT_PATHS: src/a.py\n"
        "REMAINING: none"
    ).encode()
    _install(monkeypatch, export=_export_bytes("vibe_export_success.json"), child=_child_result(chatty_block))
    payload = asyncio.run(worker.delegate_impl(rt, task="t", cwd=str(tmp_path)))
    assert len(payload["report_summary"]) <= 400
    assert payload["report_summary"] == "s" * 400
