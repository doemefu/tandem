"""The loop guard refuses tandem tools inside tandem-spawned children."""

from __future__ import annotations

import pytest

from tandem.errors import GuardRefusedError
from tandem.guard import GUARD_ENV, GUARD_MESSAGE, child_mode, refusal


def test_child_mode_reads_env() -> None:
    assert not child_mode({})
    assert child_mode({GUARD_ENV: "1"})
    assert not child_mode({GUARD_ENV: ""})
    assert not child_mode({GUARD_ENV: "true"})


def test_monkeypatched_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GUARD_ENV, raising=False)
    assert not child_mode()
    monkeypatch.setenv(GUARD_ENV, "1")
    assert child_mode()


def test_refusal_shape() -> None:
    exc = refusal("consult_claude")
    assert isinstance(exc, GuardRefusedError)
    assert exc.error_kind == "loop_guard"
    payload = exc.payload()
    assert payload["is_error"] is True
    assert payload["error_kind"] == "loop_guard"
    assert "consult_claude" in str(exc)
    assert GUARD_MESSAGE in str(exc)
