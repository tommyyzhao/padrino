"""Padrino command-line interface.

Subcommands:

- ``padrino version`` — print the installed Padrino version.
- ``padrino serve`` — launch the FastAPI app via uvicorn.
- ``padrino demo-gauntlet`` — bootstrap a SQLite-backed demo league, run a
  gauntlet, and print the resulting leaderboard JSON.
- ``padrino metrics`` — read aggregated observability metrics (game counts,
  phase durations, LLM latency percentiles, timeout / invalid-JSON rates)
  from a Padrino database and print them as JSON.
- ``padrino scheduler`` — run the async gauntlet scheduler loop until SIGTERM.
- ``padrino bootstrap`` — take a fresh database to a ready-to-serve state
  (migrations, canonical prompts, default league, optional admin key, optional
  provider registration).
- ``padrino export game`` — emit a signed JSON bundle for one completed game.
"""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path
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
    from padrino.observability.summary import (
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


@app.command("scheduler")
def scheduler(
    concurrency: int = typer.Option(
        4, "--concurrency", help="Max concurrent in-flight games across all gauntlets."
    ),
    db_url: str = typer.Option(
        "sqlite+aiosqlite:///./padrino.db",
        "--db-url",
        help="SQLAlchemy async URL for the Padrino database.",
    ),
) -> None:
    """Run the async gauntlet scheduler until SIGTERM / SIGINT."""
    from padrino.db.base import create_engine, create_session_factory
    from padrino.runner.scheduler import run_scheduler

    async def _run() -> None:
        engine = create_engine(db_url)
        try:
            session_factory = create_session_factory(engine)
            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()

            def _request_stop() -> None:
                stop_event.set()

            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _request_stop)
                except NotImplementedError:
                    # Windows: signal handlers on loops are not supported.
                    signal.signal(sig, lambda *_a: _request_stop())

            await run_scheduler(
                session_factory,
                concurrency=concurrency,
                stop_event=stop_event,
            )
        finally:
            await engine.dispose()

    asyncio.run(_run())


@app.command("bootstrap")
def bootstrap(
    db_url: str = typer.Option(
        "sqlite+aiosqlite:///./padrino.db",
        "--db-url",
        help="SQLAlchemy async URL for the Padrino database.",
    ),
    with_admin_key: bool = typer.Option(
        False,
        "--with-admin-key",
        help="Mint an admin API key and print the raw value once to stdout.",
    ),
    providers: Path | None = typer.Option(
        None,
        "--providers",
        help="Path to a YAML file declaring providers to register.",
        exists=False,
        dir_okay=False,
        file_okay=True,
        resolve_path=True,
    ),
) -> None:
    """Take a fresh database to a ready-to-serve Padrino deployment."""
    from padrino.bootstrap import bootstrap as _bootstrap

    result = asyncio.run(
        _bootstrap(
            db_url=db_url,
            with_admin_key=with_admin_key,
            providers_path=providers,
        )
    )

    payload: dict[str, Any] = {
        "succeeded": result.succeeded,
        "steps": [{"name": s.name, "status": s.status, "detail": s.detail} for s in result.steps],
    }
    if result.failed_step is not None:
        payload["failed_step"] = result.failed_step
        payload["failure_message"] = result.failure_message
    if result.admin_raw_key is not None:
        payload["admin_raw_key"] = result.admin_raw_key

    typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    if not result.succeeded:
        raise typer.Exit(code=1)


export_app = typer.Typer(
    name="export",
    help="Export Padrino artifacts (signed game bundles).",
    no_args_is_help=True,
)
app.add_typer(export_app)


@export_app.command("game")
def export_game_cmd(
    game_id: str = typer.Argument(..., help="UUID of the COMPLETED game to export."),
    db_url: str = typer.Option(
        "sqlite+aiosqlite:///./padrino.db",
        "--db-url",
        help="SQLAlchemy async URL for the Padrino database.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Write the bundle JSON to this file instead of stdout.",
        dir_okay=False,
        file_okay=True,
        resolve_path=True,
    ),
    sign: bool = typer.Option(
        False,
        "--sign",
        help=(
            "Sign the bundle with the Ed25519 seed in env "
            "PADRINO_EXPORT_PRIVATE_KEY (base64, 32 bytes)."
        ),
    ),
) -> None:
    """Emit a signed JSON game-export bundle for ``game_id``."""
    import uuid as _uuid

    from padrino.db.base import create_engine, create_session_factory
    from padrino.export.bundle import Ed25519Signer, ExportError, export_game

    try:
        gid = _uuid.UUID(game_id)
    except ValueError as exc:
        typer.echo(f"invalid game_id: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    signer = Ed25519Signer.from_env("PADRINO_EXPORT_PRIVATE_KEY") if sign else None

    async def _run() -> str:
        engine = create_engine(db_url)
        try:
            session_factory = create_session_factory(engine)
            async with session_factory() as session:
                bundle = await export_game(session, gid, signer=signer)
        finally:
            await engine.dispose()
        return bundle.model_dump_json(indent=2)

    try:
        rendered = asyncio.run(_run())
    except ExportError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if out is None:
        typer.echo(rendered)
    else:
        out.write_text(rendered, encoding="utf-8")
        typer.echo(str(out))


if __name__ == "__main__":
    app()
