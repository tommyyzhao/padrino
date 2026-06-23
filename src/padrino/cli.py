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
- ``padrino game verify-restore`` — verify a restored completed game's
  ``game_events`` hash chain against ``games.event_hash_head``.
- ``padrino smoke localhost`` — release-gate smoke that runs bootstrap, brings
  up the API + scheduler as child processes, drives a mock-adapter gauntlet
  to completion, exports + ingests one game, and asserts the documented
  shape on the public read endpoints.
"""

from __future__ import annotations

import asyncio
import json
import signal
import uuid
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
    """Configure structlog from settings so PADRINO_LOG_LEVEL takes effect."""
    from padrino.logging import configure_logging
    from padrino.settings import get_settings

    configure_logging(get_settings().padrino_log_level)


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
    db_url: str | None = typer.Option(
        None,
        "--db-url",
        envvar="PADRINO_DB_URL",
        help="SQLAlchemy async URL. Falls back to PADRINO_DB_URL / Settings default.",
    ),
) -> None:
    """Run the Padrino FastAPI app under uvicorn."""
    from padrino.api.app import create_app
    from padrino.db.base import create_engine, create_session_factory
    from padrino.settings import get_settings

    resolved_url = db_url if db_url is not None else get_settings().padrino_db_url
    engine = create_engine(resolved_url)
    session_factory = create_session_factory(engine)
    application = create_app(session_factory=session_factory)
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


gauntlet_app = typer.Typer(
    name="gauntlet",
    help="Run multi-game heterogeneous tournaments.",
    no_args_is_help=True,
)
app.add_typer(gauntlet_app, name="gauntlet")


@gauntlet_app.command("run")
def gauntlet_run(
    roster: Path = typer.Option(
        ...,
        "--roster",
        help="Roster YAML: a 'roster' mapping of public_player_id (P01..P07) -> agent_build_id.",
    ),
    league_id: str = typer.Option(..., "--league-id", help="League UUID to rate under."),
    n_games: int = typer.Option(1, "--n-games", help="Number of games to play."),
    cost_cap_usd: float = typer.Option(
        20.0,
        "--cost-cap-usd",
        help="Stop before the next game once cumulative cost exceeds this ceiling.",
    ),
    gauntlet_seed: str = typer.Option(
        "gauntlet-run-001", "--gauntlet-seed", help="Deterministic gauntlet seed."
    ),
    db_url: str = typer.Option(
        "sqlite+aiosqlite:///./padrino.db",
        "--db-url",
        envvar="PADRINO_DB_URL",
        help="SQLAlchemy async URL for the Padrino database.",
    ),
) -> None:
    """Run an N-game heterogeneous tournament from a roster YAML; print the report JSON."""
    import yaml

    from padrino.db.base import create_engine, create_session_factory
    from padrino.gauntlets.evaluation import evaluate_gauntlet
    from padrino.gauntlets.tournament import run_tournament_from_roster
    from padrino.settings import get_settings

    raw = yaml.safe_load(roster.read_text(encoding="utf-8")) or {}
    roster_section = raw.get("roster", raw) if isinstance(raw, dict) else None
    if not isinstance(roster_section, dict):
        raise typer.BadParameter(
            "roster YAML must contain a 'roster' seat -> agent_build_id mapping"
        )
    try:
        roster_by_seat = {str(seat): uuid.UUID(str(bid)) for seat, bid in roster_section.items()}
    except ValueError as exc:
        raise typer.BadParameter(f"roster agent_build_id values must be UUIDs: {exc}") from exc

    async def _run() -> dict[str, Any]:
        engine = create_engine(db_url)
        try:
            session_factory = create_session_factory(engine)
            gauntlet_id, result = await run_tournament_from_roster(
                session_factory=session_factory,
                league_id=uuid.UUID(league_id),
                gauntlet_seed=gauntlet_seed,
                roster_by_seat=roster_by_seat,
                n_games=n_games,
                settings=get_settings(),
                cost_cap_usd=cost_cap_usd,
            )
            async with session_factory() as session:
                report = await evaluate_gauntlet(gauntlet_id, session)
        finally:
            await engine.dispose()
        return {
            "gauntlet_id": str(gauntlet_id),
            "games_run": result.games_run,
            "total_cost_usd": round(result.total_cost_usd, 4),
            "cost_capped": result.cost_capped,
            "report": report.model_dump(mode="json") if report is not None else None,
        }

    typer.echo(json.dumps(asyncio.run(_run()), indent=2, sort_keys=True))


@app.command("scheduler")
def scheduler(
    concurrency: int = typer.Option(
        4, "--concurrency", help="Max concurrent in-flight games across all gauntlets."
    ),
    db_url: str = typer.Option(
        "sqlite+aiosqlite:///./padrino.db",
        "--db-url",
        envvar="PADRINO_DB_URL",
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

            from padrino.scheduler.bootstrap import build_scheduled_gauntlet_tick_hook
            from padrino.settings import get_settings

            settings = get_settings()
            guard = None
            if settings.padrino_enable_continuous_matchmaking:
                from padrino.public.moderation import build_guard_from_settings

                guard = build_guard_from_settings(settings)
                if guard is None:
                    typer.echo(
                        "WARNING: continuous matchmaking is enabled but no "
                        "DEEPINFRA_API_KEY resolves — the moderation gate fails "
                        "closed and NO game will be broadcastable.",
                        err=True,
                    )
            from padrino.observability.alerts import build_alert_notifier

            notifier = build_alert_notifier(settings)
            tick_hook = build_scheduled_gauntlet_tick_hook(
                session_factory, settings=settings, guard=guard, notifier=notifier
            )

            await run_scheduler(
                session_factory,
                concurrency=concurrency,
                stop_event=stop_event,
                tick_hook=tick_hook,
            )
        finally:
            await engine.dispose()

    asyncio.run(_run())


@app.command("human-lane")
def human_lane(
    concurrency: int | None = typer.Option(
        None,
        "--concurrency",
        help=(
            "Max concurrent in-flight human games on this lane. Defaults to "
            "padrino_human_lane_max_concurrent."
        ),
    ),
    db_url: str = typer.Option(
        "sqlite+aiosqlite:///./padrino.db",
        "--db-url",
        envvar="PADRINO_DB_URL",
        help="SQLAlchemy async URL for the Padrino database.",
    ),
) -> None:
    """Run the isolated human-game worker lane until SIGTERM / SIGINT.

    This is a SEPARATE process from ``padrino scheduler``: it drains human-lane
    games (seats occupied by humans) under its own concurrency cap, so
    minutes-to-hours human games never starve the benchmark scheduler.
    """
    from padrino.db.base import create_engine, create_session_factory
    from padrino.runner.human_lane import run_human_lane
    from padrino.settings import get_settings

    resolved_concurrency = (
        concurrency if concurrency is not None else get_settings().padrino_human_lane_max_concurrent
    )

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

            await run_human_lane(
                session_factory,
                concurrency=resolved_concurrency,
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
        envvar="PADRINO_DB_URL",
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


game_app = typer.Typer(
    name="game",
    help="Manage and evaluate individual games.",
    no_args_is_help=True,
)
app.add_typer(game_app, name="game")


@game_app.command("verify-restore")
def game_verify_restore(
    game_id: str = typer.Argument(..., help="UUID of the restored COMPLETED game to verify."),
    db_url: str = typer.Option(
        "sqlite+aiosqlite:///./padrino.db",
        "--db-url",
        envvar="PADRINO_DB_URL",
        help="SQLAlchemy async URL for the restored Padrino database.",
    ),
) -> None:
    """Verify one restored game's persisted hash-chain rows."""
    import uuid as _uuid

    from padrino.core.engine.replay import ReplayHashMismatchError
    from padrino.db.base import create_engine, create_session_factory
    from padrino.ops.backup_restore import (
        RestoreVerification,
        RestoreVerificationError,
        verify_restored_game_hash_chain,
    )

    try:
        gid = _uuid.UUID(game_id)
    except ValueError as exc:
        typer.echo(f"invalid game_id: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    async def _run() -> RestoreVerification:
        engine = create_engine(db_url)
        try:
            session_factory = create_session_factory(engine)
            async with session_factory() as session:
                return await verify_restored_game_hash_chain(session, gid)
        finally:
            await engine.dispose()

    try:
        result = asyncio.run(_run())
    except (ReplayHashMismatchError, RestoreVerificationError) as exc:
        typer.echo(
            json.dumps(
                {"game_id": game_id, "status": "failed", "error": str(exc)},
                indent=2,
                sort_keys=True,
            ),
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(
        json.dumps(
            {
                "event_count": result.event_count,
                "final_event_type": result.final_event_type,
                "game_id": str(result.game_id),
                "status": "ok",
                "tip_hash": result.tip_hash,
            },
            indent=2,
            sort_keys=True,
        )
    )


@game_app.command("evaluate-behavioral")
def game_evaluate_behavioral(
    game_id: str = typer.Argument(..., help="UUID of the COMPLETED game to evaluate."),
    db_url: str = typer.Option(
        "sqlite+aiosqlite:///./padrino.db",
        "--db-url",
        envvar="PADRINO_DB_URL",
        help="SQLAlchemy async URL for the Padrino database.",
    ),
) -> None:
    """Run post-game LLM judge behavioral evaluation for a completed game."""
    import uuid as _uuid

    from padrino.db.base import create_engine, create_session_factory
    from padrino.ratings.evaluator import evaluate_completed_game_behavioral

    try:
        gid = _uuid.UUID(game_id)
    except ValueError as exc:
        typer.echo(f"invalid game_id: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    async def _run() -> int:
        engine = create_engine(db_url)
        try:
            session_factory = create_session_factory(engine)
            async with session_factory() as session, session.begin():
                persisted = await evaluate_completed_game_behavioral(session, gid)
                return len(persisted)
        finally:
            await engine.dispose()

    try:
        count = asyncio.run(_run())
        typer.echo(
            json.dumps(
                {"game_id": game_id, "status": "success", "evaluations_persisted": count}, indent=2
            )
        )
    except Exception as exc:
        typer.echo(
            json.dumps({"game_id": game_id, "status": "failed", "error": str(exc)}, indent=2),
            err=True,
        )
        raise typer.Exit(code=1) from exc


smoke_app = typer.Typer(
    name="smoke",
    help="End-to-end smoke harnesses for release-gate validation.",
    no_args_is_help=True,
)
app.add_typer(smoke_app)


@smoke_app.command("localhost")
def smoke_localhost(
    db_url: str = typer.Option(
        "sqlite+aiosqlite:///./padrino-smoke.db",
        "--db-url",
        envvar="PADRINO_DB_URL",
        help="SQLAlchemy async URL for the smoke database.",
    ),
    port: int = typer.Option(8000, "--port", help="TCP port for the spawned API child."),
    keep_running: bool = typer.Option(
        False,
        "--keep-running",
        help="Skip teardown of the API + scheduler children on success.",
    ),
    clone_count: int = typer.Option(
        1, "--clone-count", help="Number of child games to schedule in the smoke gauntlet."
    ),
    timeout_s: float = typer.Option(
        120.0,
        "--timeout-s",
        help="Maximum seconds to wait for the gauntlet to reach COMPLETED.",
    ),
) -> None:
    """Run the localhost end-to-end smoke harness (US-068)."""
    from padrino.smoke import run_smoke_subprocess

    result = asyncio.run(
        run_smoke_subprocess(
            db_url=db_url,
            port=port,
            keep_running=keep_running,
            clone_count=clone_count,
            gauntlet_timeout_s=timeout_s,
        )
    )
    typer.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    if not result.succeeded:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
