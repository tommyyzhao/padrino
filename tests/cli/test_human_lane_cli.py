"""CLI wiring tests for the isolated human-lane worker."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from padrino import settings as settings_module
from padrino.cli import app
from padrino.llm.mock import NoopMockAdapter


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def test_human_lane_cli_mock_ai_flag_injects_noop_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    engine = _FakeEngine()

    async def fake_run_human_lane(*args: Any, **kwargs: Any) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs
        factory = kwargs["ai_adapter_factory"]
        captured["mock_adapter"] = factory({})

    monkeypatch.setenv("PADRINO_HUMAN_LANE_MOCK_AI", "1")
    settings_module.get_settings.cache_clear()
    monkeypatch.setattr("padrino.db.base.create_engine", lambda _db_url: engine)
    monkeypatch.setattr("padrino.db.base.create_session_factory", lambda _engine: "sessions")
    monkeypatch.setattr("padrino.runner.human_lane.run_human_lane", fake_run_human_lane)

    try:
        result = CliRunner().invoke(
            app,
            [
                "human-lane",
                "--db-url",
                "sqlite+aiosqlite:///test.db",
                "--concurrency",
                "1",
            ],
        )
    finally:
        settings_module.get_settings.cache_clear()

    assert result.exit_code == 0, result.output
    assert captured["args"] == ("sessions",)
    assert captured["kwargs"]["concurrency"] == 1
    assert captured["kwargs"]["settings"].padrino_human_lane_mock_ai is True
    assert isinstance(captured["mock_adapter"], NoopMockAdapter)
    assert engine.disposed is True


def test_human_lane_cli_default_uses_production_ai_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    engine = _FakeEngine()

    async def fake_run_human_lane(*_args: Any, **kwargs: Any) -> None:
        captured["kwargs"] = kwargs

    monkeypatch.delenv("PADRINO_HUMAN_LANE_MOCK_AI", raising=False)
    settings_module.get_settings.cache_clear()
    monkeypatch.setattr("padrino.db.base.create_engine", lambda _db_url: engine)
    monkeypatch.setattr("padrino.db.base.create_session_factory", lambda _engine: "sessions")
    monkeypatch.setattr("padrino.runner.human_lane.run_human_lane", fake_run_human_lane)

    try:
        result = CliRunner().invoke(
            app,
            [
                "human-lane",
                "--db-url",
                "sqlite+aiosqlite:///test.db",
                "--concurrency",
                "1",
            ],
        )
    finally:
        settings_module.get_settings.cache_clear()

    assert result.exit_code == 0, result.output
    assert captured["kwargs"]["ai_adapter_factory"] is None
    assert captured["kwargs"]["settings"].padrino_human_lane_mock_ai is False
    assert engine.disposed is True
