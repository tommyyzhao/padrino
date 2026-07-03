"""US-295: ranked-audit is registered on the existing Typer app."""

from __future__ import annotations

import click
import typer
from typer.testing import CliRunner

from padrino.cli import app


def test_ranked_audit_command_is_registered_on_existing_cli_app() -> None:
    # The command exists on the existing app and is invokable.
    result = CliRunner().invoke(app, ["ranked-audit", "--help"])
    assert result.exit_code == 0

    # Introspect the Click command instead of grepping the Rich-rendered help
    # text: Rich wraps option names across lines at a narrow / no-TTY terminal
    # width (as on CI), so a substring check on result.output is width-fragile
    # (it passes at a wide local width and fails at 80 cols on CI).
    command = typer.main.get_command(app)
    assert isinstance(command, click.Group)
    ranked_audit = command.commands["ranked-audit"]
    assert (ranked_audit.help or "").startswith("Audit ranked human-rating rows")
    option_names = {opt for param in ranked_audit.params for opt in getattr(param, "opts", [])}
    assert "--db-url" in option_names
