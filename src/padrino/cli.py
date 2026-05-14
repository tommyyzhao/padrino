"""Padrino command-line interface.

Subcommands:

- ``padrino version`` — print the installed Padrino version.
- ``padrino serve`` — launch the FastAPI app via uvicorn.

Further subcommands (demo-gauntlet, replay, metrics) land in later stories.
"""

from __future__ import annotations

import typer
import uvicorn

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


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Interface to bind."),
    port: int = typer.Option(8000, help="TCP port to listen on."),
    reload: bool = typer.Option(False, help="Enable uvicorn auto-reload (dev only)."),
    log_level: str = typer.Option("info", help="uvicorn log level."),
) -> None:
    """Run the Padrino FastAPI app under uvicorn."""
    from padrino.api.app import create_app

    application = create_app()
    uvicorn.run(application, host=host, port=port, reload=reload, log_level=log_level)


if __name__ == "__main__":
    app()
