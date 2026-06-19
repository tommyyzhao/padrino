"""CLI tests asserting PADRINO_LOG_LEVEL is honored (US-115).

The Typer callback configures structlog for every subcommand; it must read the
configured level from ``settings.padrino_log_level`` (env ``PADRINO_LOG_LEVEL``)
rather than a hardcoded literal, so operators can raise verbosity in prod.
"""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from padrino.cli import app

runner = CliRunner()


def _invoke_capturing_level(args: list[str]) -> str | None:
    """Invoke ``args`` and return the level passed to ``configure_logging``."""
    captured: dict[str, str] = {}

    def _fake_configure(level: str = "INFO") -> None:
        captured["level"] = level

    # Stub out the heavy command bodies; we only care about the callback.
    with (
        patch("padrino.logging.configure_logging", _fake_configure),
        patch("uvicorn.run"),
        patch("padrino.api.app.create_app"),
        patch("padrino.db.base.create_engine"),
        patch("padrino.db.base.create_session_factory"),
        patch("asyncio.run"),
    ):
        runner.invoke(app, args)
    return captured.get("level")


def test_serve_configures_logging_from_settings(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PADRINO_LOG_LEVEL", "DEBUG")
    from padrino import settings as settings_module

    settings_module.get_settings.cache_clear()
    level = _invoke_capturing_level(["serve"])
    assert level == "DEBUG"


def test_scheduler_configures_logging_from_settings(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PADRINO_LOG_LEVEL", "DEBUG")
    from padrino import settings as settings_module

    settings_module.get_settings.cache_clear()
    level = _invoke_capturing_level(["scheduler"])
    assert level == "DEBUG"


def test_bootstrap_configures_logging_from_settings(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PADRINO_LOG_LEVEL", "WARNING")
    from padrino import settings as settings_module

    settings_module.get_settings.cache_clear()
    level = _invoke_capturing_level(["bootstrap"])
    assert level == "WARNING"
