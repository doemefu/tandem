"""Bootstrap baseline: package metadata and the captured claude -p result fixture."""

from __future__ import annotations

import json
from pathlib import Path

import tandem.__main__ as tandem_main
from tandem import ROLES, __version__

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
USAGE_LIMIT_FIXTURE = FIXTURES_DIR / "claude_print_usage_limit.json"


def test_roles_are_pinned() -> None:
    assert ROLES == ("thinker", "worker")


def test_version_is_three_part() -> None:
    assert __version__.count(".") == 2


def test_main_rejects_missing_role(monkeypatch, capsys) -> None:
    monkeypatch.delenv("TANDEM_ROLE", raising=False)
    assert tandem_main.main() == tandem_main.EXIT_CONFIG_ERROR
    assert "TANDEM_ROLE" in capsys.readouterr().err


def test_main_rejects_unknown_role(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TANDEM_ROLE", "supervisor")
    assert tandem_main.main() == tandem_main.EXIT_CONFIG_ERROR
    assert "supervisor" in capsys.readouterr().err


def test_main_accepts_both_roles(monkeypatch) -> None:
    started: list[str] = []

    async def fake_serve(cfg: object) -> None:
        started.append("served")  # pragma: no cover - assertion happens outside

    monkeypatch.setattr(tandem_main, "_serve", fake_serve)
    for role in ROLES:
        monkeypatch.setenv("TANDEM_ROLE", role)
        assert tandem_main.main() == 0
    assert started == ["served", "served"]


def test_usage_limit_fixture_pins_the_result_contract() -> None:
    """The live-captured quota failure anchors the parser contract.

    Fields observed on claude 2.1.321 (2026-11-11): a quota rejection carries
    ``is_error=True`` and ``api_error=usage_limit_reached`` while ``subtype``
    still says "success" - tolerant parsing is mandatory.
    """
    data = json.loads(USAGE_LIMIT_FIXTURE.read_text(encoding="utf-8"))
    assert data["type"] == "result"
    assert data["is_error"] is True
    assert data["api_error"] == "usage_limit_reached"
    assert data["api_error_status"] == 429
    assert data["subtype"] == "success"
    assert "weekly limit" in data["result"]
    assert isinstance(data["session_id"], str)
    assert data["session_id"]
