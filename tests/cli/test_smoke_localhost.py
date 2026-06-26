"""Tests for the ``padrino smoke localhost`` harness (US-068).

The default-runnable path exercises :func:`padrino.smoke.run_smoke_in_process`
against a SQLite database — no subprocesses, no Postgres, no network. The
in-process variant runs the API via :class:`httpx.ASGITransport` and the
scheduler as an ``asyncio.Task`` in the same event loop, so a single pytest
invocation can validate the full wiring.

The full subprocess + Postgres path is guarded by
:mod:`@pytest.mark.integration` so CI's default ``-m "not integration"``
selection skips it; running it locally requires a reachable Postgres URL
in ``PADRINO_SMOKE_PG_URL``.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import padrino.smoke as smoke_module
from padrino.bootstrap import BootstrapResult, StepReport
from padrino.cli import app
from padrino.smoke import (
    STEP_ASSERT_LEAGUE_LEADERBOARD,
    STEP_ASSERT_PUBLIC_EVENTS,
    STEP_ASSERT_PUBLIC_LEADERBOARD,
    STEP_ASSERT_PUBLIC_MODELS,
    STEP_BOOTSTRAP,
    STEP_EXPORT_INGEST,
    STEP_HEALTHZ,
    STEP_HEALTHZ_SCHEDULER,
    STEP_SEED_ADMIN,
    STEP_SUBMIT_GAUNTLET,
    STEP_WAIT_COMPLETED,
    run_smoke_in_process,
    run_smoke_subprocess,
)


def _db_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'smoke.db'}"


def _step(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [s for s in steps if s["name"] == name]
    assert matches, f"step {name!r} missing from {[s['name'] for s in steps]!r}"
    return matches[0]


def test_run_smoke_in_process_full_flow_succeeds(tmp_path: Path) -> None:
    result = asyncio.run(
        run_smoke_in_process(
            db_url=_db_url(tmp_path),
            clone_count=1,
            gauntlet_timeout_s=60.0,
        )
    )

    assert result.succeeded, result.to_dict()
    assert result.failed_step is None
    assert result.admin_raw_key is not None and result.admin_raw_key.startswith("pk_")
    assert result.league_id is not None
    assert result.gauntlet_id is not None
    assert result.ingested_game_id is not None

    step_names = [s.name for s in result.steps]
    # Every required step ran in the documented order.
    expected_order = [
        STEP_BOOTSTRAP,
        STEP_HEALTHZ,
        STEP_HEALTHZ_SCHEDULER,
        STEP_SEED_ADMIN,
        STEP_SUBMIT_GAUNTLET,
        STEP_WAIT_COMPLETED,
        STEP_EXPORT_INGEST,
        STEP_ASSERT_LEAGUE_LEADERBOARD,
        STEP_ASSERT_PUBLIC_LEADERBOARD,
        STEP_ASSERT_PUBLIC_MODELS,
        STEP_ASSERT_PUBLIC_EVENTS,
    ]
    assert step_names == expected_order

    for step in result.steps:
        assert step.status == "ok", (step.name, step.detail)

    payload = result.to_dict()
    league_entries = _step(payload["steps"], STEP_ASSERT_LEAGUE_LEADERBOARD)["detail"]
    public_entries = _step(payload["steps"], STEP_ASSERT_PUBLIC_LEADERBOARD)["detail"]
    models_entries = _step(payload["steps"], STEP_ASSERT_PUBLIC_MODELS)["detail"]
    events_entries = _step(payload["steps"], STEP_ASSERT_PUBLIC_EVENTS)["detail"]
    assert int(league_entries["entries"]) >= 1
    assert int(public_entries["entries"]) >= 1
    assert int(models_entries["entries"]) >= 1
    assert int(events_entries["events"]) >= 1


def test_run_smoke_in_process_serializes_to_json(tmp_path: Path) -> None:
    result = asyncio.run(run_smoke_in_process(db_url=_db_url(tmp_path), clone_count=1))
    payload = result.to_dict()
    # to_dict() must be JSON-serializable so the CLI subcommand can emit it.
    rendered = json.dumps(payload)
    parsed = json.loads(rendered)
    assert parsed["succeeded"] is True
    assert "admin_raw_key" in parsed
    assert "steps" in parsed and isinstance(parsed["steps"], list)


def test_smoke_localhost_cli_help() -> None:
    import click
    import typer

    runner = CliRunner()
    result = runner.invoke(app, ["smoke", "localhost", "--help"])
    assert result.exit_code == 0
    assert "smoke" in result.stdout.lower()
    # Assert the command DECLARES its key options by introspecting the command
    # tree rather than scraping the rendered help text. The rich/Typer help
    # renderer wraps/elides option names at the (narrow, non-TTY) CI terminal
    # width, so substring checks against result.stdout are flaky across
    # environments; introspection is rendering-independent.
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    smoke = root.commands["smoke"]
    assert isinstance(smoke, click.Group)
    localhost_cmd = smoke.commands["localhost"]
    option_names = {opt for param in localhost_cmd.params for opt in param.opts}
    assert {"--db-url", "--port", "--keep-running", "--with-human-lane"} <= option_names


class _FakeEngine:
    async def dispose(self) -> None:
        return None


class _FakeProcess:
    def __init__(self, cmd: list[str], *, env: dict[str, str]) -> None:
        self.cmd = cmd
        self.env = env
        self.stderr = io.BytesIO()
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return 0 if self.terminated or self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


async def _bootstrap_ok(*_args: Any, **_kwargs: Any) -> BootstrapResult:
    return BootstrapResult(
        succeeded=True,
        steps=(StepReport(name="bootstrap", status="ok"),),
        admin_raw_key="pk_smoke",
    )


async def _execute_smoke_flow_noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def test_run_smoke_subprocess_starts_human_lane_child_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[_FakeProcess] = []

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeProcess:
        proc = _FakeProcess(cmd, env=kwargs["env"])
        processes.append(proc)
        return proc

    monkeypatch.setattr(smoke_module, "bootstrap", _bootstrap_ok)
    monkeypatch.setattr(smoke_module, "_execute_smoke_flow", _execute_smoke_flow_noop)
    monkeypatch.setattr(smoke_module, "create_engine", lambda _db_url: _FakeEngine())
    monkeypatch.setattr(smoke_module, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr("padrino.smoke.subprocess.Popen", fake_popen)

    result = asyncio.run(
        run_smoke_subprocess(
            db_url="sqlite+aiosqlite:///smoke.db",
            port=8123,
            with_human_lane=True,
            keep_running=False,
        )
    )

    assert result.succeeded is True
    assert [proc.cmd[3] for proc in processes] == ["serve", "scheduler", "human-lane"]
    human_lane_proc = processes[2]
    assert human_lane_proc.cmd[-2:] == ["--db-url", "sqlite+aiosqlite:///smoke.db"]
    assert human_lane_proc.env["PADRINO_DB_URL"] == "sqlite+aiosqlite:///smoke.db"
    assert human_lane_proc.env["PADRINO_HUMAN_LANE_MOCK_AI"] == "true"
    assert all(proc.terminated for proc in processes)


def test_run_smoke_subprocess_leaves_human_lane_child_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[_FakeProcess] = []

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeProcess:
        proc = _FakeProcess(cmd, env=kwargs["env"])
        processes.append(proc)
        return proc

    monkeypatch.setattr(smoke_module, "bootstrap", _bootstrap_ok)
    monkeypatch.setattr(smoke_module, "_execute_smoke_flow", _execute_smoke_flow_noop)
    monkeypatch.setattr(smoke_module, "create_engine", lambda _db_url: _FakeEngine())
    monkeypatch.setattr(smoke_module, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr("padrino.smoke.subprocess.Popen", fake_popen)

    result = asyncio.run(
        run_smoke_subprocess(
            db_url="sqlite+aiosqlite:///smoke.db",
            port=8123,
            keep_running=False,
        )
    )

    assert result.succeeded is True
    assert [proc.cmd[3] for proc in processes] == ["serve", "scheduler"]


def test_dashboard_global_setup_enables_human_lane_worker() -> None:
    source = Path("web/dashboard/tests/e2e/global-setup.ts").read_text(encoding="utf-8")
    assert "'--with-human-lane'" in source


def test_run_smoke_in_process_fails_fast_on_bootstrap_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bootstrap failure surfaces as a non-zero result with the failing step.

    Forcing a failure via an unwritable directory keeps the test
    deterministic without monkeypatching internals.
    """
    bad_path = tmp_path / "missing" / "child" / "smoke.db"
    db_url = f"sqlite+aiosqlite:///{bad_path}"

    result = asyncio.run(
        run_smoke_in_process(
            db_url=db_url,
            clone_count=1,
            gauntlet_timeout_s=10.0,
            health_timeout_s=2.0,
        )
    )
    assert result.succeeded is False
    assert result.failed_step is not None


@pytest.mark.integration
def test_run_smoke_subprocess_full_flow_against_postgres(tmp_path: Path) -> None:
    """Run the full subprocess + Postgres path. Opt-in via ``-m integration``.

    Requires ``PADRINO_SMOKE_PG_URL`` to point at a reachable Postgres
    instance (asyncpg async URL). Skipped when the env var is unset so
    the integration suite stays runnable without one.
    """
    pg_url = os.environ.get("PADRINO_SMOKE_PG_URL")
    if not pg_url:
        pytest.skip("PADRINO_SMOKE_PG_URL not set")

    result = asyncio.run(
        run_smoke_subprocess(
            db_url=pg_url,
            keep_running=False,
            clone_count=1,
        )
    )
    assert result.succeeded, result.to_dict()
    assert result.admin_raw_key is not None and result.admin_raw_key.startswith("pk_")
