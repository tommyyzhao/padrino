"""Padrino command-line interface.

Stub entry point. Real subcommands (demo-gauntlet, replay, etc.) are added
in later user stories.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="padrino",
    help="Padrino: deterministic LLM benchmark engine for Mafia-style social deduction.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the installed Padrino version."""
    from padrino import __version__

    typer.echo(__version__)


if __name__ == "__main__":
    app()
