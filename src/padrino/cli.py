"""Padrino command-line interface.

Subcommands:

- ``padrino version`` — print the installed Padrino version.
- ``padrino serve`` — launch the FastAPI app via uvicorn.
- ``padrino demo-gauntlet`` — bootstrap a SQLite-backed demo league, run a
  gauntlet, and print the resulting leaderboard JSON.
- ``padrino metrics`` — read aggregated observability metrics (game counts,
  phase durations, LLM latency percentiles, timeout / invalid-JSON rates)
  from a Padrino database and print them as JSON.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import typer
import uvicorn

app = typer.Typer(
    name="padrino",
    help="Padrino: deterministic LLM benchmark engine for Mafia-style social deduction.",
    no_args_is_help=True,
)


@app.callback()
def _configure_logging_callback() -> None:
    """Route structlog INFO events to stderr so stdout only carries CLI payloads."""
    from padrino.logging import configure_logging

    configure_logging("INFO")


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


@app.command("metrics")
def metrics(
    db_url: str = typer.Option(
        "sqlite+aiosqlite:///./padrino-demo.db",
        "--db-url",
        help="SQLAlchemy async URL for the Padrino database.",
    ),
) -> None:
    """Print aggregated observability metrics as JSON."""
    from padrino.db.base import create_engine, create_session_factory
    from padrino.observability.metrics import (
        compute_metrics_summary,
        metrics_summary_to_dict,
    )

    async def _run() -> dict[str, Any]:
        engine = create_engine(db_url)
        try:
            session_factory = create_session_factory(engine)
            async with session_factory() as session:
                summary = await compute_metrics_summary(session)
        finally:
            await engine.dispose()
        return metrics_summary_to_dict(summary)

    payload = asyncio.run(_run())
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("demo-gauntlet")
def demo_gauntlet(
    seed: str = typer.Option("demo-seed-001", "--seed", help="Gauntlet seed."),
    real: bool = typer.Option(
        False, "--real", help="Use the real LiteLLM adapter instead of the mock."
    ),
    clones: int = typer.Option(5, "--clones", help="Number of child games to schedule."),
    db_url: str = typer.Option(
        "sqlite+aiosqlite:///./padrino-demo.db",
        "--db-url",
        help="SQLAlchemy async URL for the demo database.",
    ),
) -> None:
    """Run a self-contained demo gauntlet and print the leaderboard JSON."""
    from padrino.demo_gauntlet import run_demo_gauntlet

    response = asyncio.run(run_demo_gauntlet(seed=seed, clones=clones, db_url=db_url, real=real))
    typer.echo(json.dumps(response, indent=2, sort_keys=True))


if __name__ == "__main__":
    app()
