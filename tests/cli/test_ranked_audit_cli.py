"""US-295: ranked-audit is registered on the existing Typer app."""

from __future__ import annotations

from typer.testing import CliRunner

from padrino.cli import app


def test_ranked_audit_command_is_registered_on_existing_cli_app() -> None:
    result = CliRunner().invoke(app, ["ranked-audit", "--help"])

    assert result.exit_code == 0
    assert "Audit ranked human-rating rows" in result.output
    assert "--db-url" in result.output
