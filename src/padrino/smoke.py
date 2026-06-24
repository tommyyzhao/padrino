"""Localhost end-to-end QA smoke harness (US-068).

``padrino smoke localhost`` is the single-command release gate: it walks a
fresh database through bootstrap, brings up the API + scheduler, drives a
mini-7 gauntlet through the deterministic mock adapter, exports + ingests
one completed game, and finally asserts the documented response shape on
``/leagues/{id}/leaderboard``, ``/public/leaderboard``,
``/public/models/leaderboard``, and ``/public/games/{id}/events``.

Two execution modes share the same flow:

* :func:`run_smoke_in_process` — used by the unit test and any caller that
  wants a self-contained, single-process check (SQLite by default). The API
  is reached via :class:`httpx.ASGITransport`; the scheduler runs as an
  ``asyncio.Task`` in the same event loop.
* :func:`run_smoke_subprocess` — used by the CLI. ``padrino serve`` and
  ``padrino scheduler`` run as child processes; the smoke talks to them
  over real HTTP on ``127.0.0.1:{port}``. Children are torn down before
  returning unless ``keep_running=True``.

All LLM calls go through :class:`padrino.llm.mock.NoopMockAdapter` — no
network. The mock takes every game to a ``MAX_DAYS_REACHED`` draw, which is
enough to exercise the runner, ratings, exports, and the public ingestion
path end-to-end.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
import structlog
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.bootstrap import (
    BootstrapResult,
    bootstrap,
)
from padrino.core.rulesets import mini7_v1
from padrino.db.base import create_engine, create_session_factory
from padrino.export.bundle import Ed25519Signer, export_game
from padrino.runner.scheduler import run_scheduler

_LOG = structlog.get_logger(__name__)

# Step identifiers — kept stable so callers (tests, dashboards) can match
# specific steps in the structured report.
STEP_BOOTSTRAP: Final[str] = "bootstrap"
STEP_HEALTHZ: Final[str] = "healthz"
STEP_HEALTHZ_SCHEDULER: Final[str] = "healthz_scheduler"
STEP_SEED_ADMIN: Final[str] = "seed_admin_entities"
STEP_SUBMIT_GAUNTLET: Final[str] = "submit_gauntlet"
STEP_WAIT_COMPLETED: Final[str] = "wait_gauntlet_completed"
STEP_EXPORT_INGEST: Final[str] = "export_and_ingest"
STEP_ASSERT_LEAGUE_LEADERBOARD: Final[str] = "assert_league_leaderboard"
STEP_ASSERT_PUBLIC_LEADERBOARD: Final[str] = "assert_public_leaderboard"
STEP_ASSERT_PUBLIC_MODELS: Final[str] = "assert_public_models_leaderboard"
STEP_ASSERT_PUBLIC_EVENTS: Final[str] = "assert_public_game_events"

DEFAULT_GAUNTLET_TIMEOUT_S: Final[float] = 120.0
DEFAULT_HEALTH_TIMEOUT_S: Final[float] = 30.0
DEFAULT_HEALTH_POLL_INTERVAL_S: Final[float] = 0.25
DEFAULT_GAUNTLET_POLL_INTERVAL_S: Final[float] = 0.5
DEFAULT_BOOT_TIMEOUT_S: Final[float] = 30.0

_REQUIRED_LEAGUE_LEADERBOARD_KEYS: Final[frozenset[str]] = frozenset(
    {"leaderboard_id", "ruleset_id", "prompt_version", "rating_model", "entries"}
)
_REQUIRED_LEAGUE_ENTRY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "agent_build_id",
        "display_name",
        "games",
        "wins",
        "draws",
        "losses",
        "mu",
        "sigma",
        "conservative_score",
    }
)
_REQUIRED_PUBLIC_LEADERBOARD_KEYS: Final[frozenset[str]] = frozenset(
    {"ruleset_id", "rating_model", "cache_tag", "entries", "total_estimate"}
)
_REQUIRED_PUBLIC_ENTRY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "entity_id",
        "display_name",
        "model_provider",
        "model_name",
        "prompt_version",
        "games",
        "wins",
        "draws",
        "losses",
        "mu",
        "sigma",
        "conservative_score",
    }
)
_REQUIRED_MODELS_LEADERBOARD_KEYS: Final[frozenset[str]] = frozenset(
    {"league_id", "ruleset_id", "rating_model", "cache_tag", "entries", "total_estimate"}
)
_REQUIRED_MODELS_ENTRY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "model_key",
        "display_name",
        "model_provider",
        "model_name",
        "mu",
        "sigma",
        "conservative_score",
        "games",
        "town",
        "mafia",
    }
)
_REQUIRED_EVENTS_KEYS: Final[frozenset[str]] = frozenset({"game_id", "items", "total_estimate"})
_REQUIRED_EVENT_ITEM_KEYS: Final[frozenset[str]] = frozenset(
    {"sequence", "event_type", "phase", "visibility", "payload", "prev_event_hash", "event_hash"}
)


class SmokeError(RuntimeError):
    """Raised when a smoke step fails. Carries step + message for reporting."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"{step}: {message}")
        self.step = step
        self.message = message


@dataclass(frozen=True)
class SmokeStepReport:
    name: str
    status: str  # "ok" | "failed" | "skipped"
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SmokeResult:
    succeeded: bool
    steps: tuple[SmokeStepReport, ...]
    failed_step: str | None = None
    failure_message: str | None = None
    admin_raw_key: str | None = None
    league_id: str | None = None
    gauntlet_id: str | None = None
    ingested_game_id: str | None = None
    stderr_tail: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "succeeded": self.succeeded,
            "steps": [{"name": s.name, "status": s.status, "detail": s.detail} for s in self.steps],
        }
        if self.failed_step is not None:
            payload["failed_step"] = self.failed_step
            payload["failure_message"] = self.failure_message
        if self.admin_raw_key is not None:
            payload["admin_raw_key"] = self.admin_raw_key
        if self.league_id is not None:
            payload["league_id"] = self.league_id
        if self.gauntlet_id is not None:
            payload["gauntlet_id"] = self.gauntlet_id
        if self.ingested_game_id is not None:
            payload["ingested_game_id"] = self.ingested_game_id
        if self.stderr_tail:
            payload["stderr_tail"] = list(self.stderr_tail)
        return payload


@dataclass
class _SmokeContext:
    steps: list[SmokeStepReport] = field(default_factory=list)
    admin_raw_key: str | None = None
    league_id: str | None = None
    gauntlet_id: str | None = None
    ingested_game_id: str | None = None
    stderr_tail: list[str] = field(default_factory=list)
    signer: Any = None
    submitter_raw_key: str | None = None

    def add(self, name: str, status: str, **detail: Any) -> None:
        self.steps.append(SmokeStepReport(name=name, status=status, detail=detail))


async def _wait_for_health(
    client: AsyncClient,
    *,
    timeout_s: float,
    poll_interval_s: float,
) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            resp = await client.get("/healthz")
            if resp.status_code == 200:
                return
            last_error = f"status={resp.status_code}"
        except httpx.HTTPError as exc:
            last_error = type(exc).__name__
        await asyncio.sleep(poll_interval_s)
    raise SmokeError(STEP_HEALTHZ, f"timeout after {timeout_s:.1f}s (last={last_error!r})")


async def _wait_for_scheduler_health(
    client: AsyncClient,
    *,
    timeout_s: float,
    poll_interval_s: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_status: str | None = None
    last_body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            resp = await client.get("/healthz/scheduler")
            if resp.status_code == 200:
                body = resp.json()
                if isinstance(body, dict):
                    last_body = body
                    last_status = str(body.get("status"))
                    if last_status == "ok":
                        return body
        except httpx.HTTPError as exc:
            last_status = type(exc).__name__
        await asyncio.sleep(poll_interval_s)
    raise SmokeError(
        STEP_HEALTHZ_SCHEDULER,
        f"scheduler readiness never reached 'ok' (last status={last_status!r}, body={last_body!r})",
    )


def _admin_headers(admin_raw_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_raw_key}"} if admin_raw_key else {}


async def _seed_admin_entities(
    client: AsyncClient,
    *,
    ctx: _SmokeContext,
    headers: dict[str, str],
) -> tuple[str, str, list[str]]:
    """Create provider + model_config + prompt + 7-slot roster.

    Returns ``(league_id, prompt_version_id, roster)``. The default league
    created by ``padrino bootstrap`` is reused; a new prompt_version + agent
    builds are registered so the gauntlet POST has identifiers to point at.
    """
    pr = await client.post(
        "/model-providers",
        json={"name": f"smoke-{uuid.uuid4().hex[:8]}", "auth_secret_ref": "env:PATH"},
        headers=headers,
    )
    if pr.status_code != 201:
        raise SmokeError(STEP_SEED_ADMIN, f"create provider failed: {pr.status_code} {pr.text}")
    provider_id = pr.json()["id"]

    mc = await client.post(
        "/model-configs",
        json={
            "provider_id": provider_id,
            "model_name": "smoke-mock",
            "model_version": "1",
            "default_temperature": 0.0,
            "default_top_p": 1.0,
            "default_max_output_tokens": 1024,
            "supports_structured_outputs": True,
        },
        headers=headers,
    )
    if mc.status_code != 201:
        raise SmokeError(STEP_SEED_ADMIN, f"create model_config failed: {mc.status_code} {mc.text}")
    mc_id = mc.json()["id"]

    pv = await client.post(
        "/prompt-versions",
        json={
            "ruleset_id": mini7_v1.RULESET_ID,
            "version": f"smoke-{uuid.uuid4().hex[:8]}",
            "system_prompt": "smoke",
            "developer_prompt": "smoke",
            "response_schema": {"type": "object"},
            "prompt_hash": f"smoke-{uuid.uuid4().hex}",
        },
        headers=headers,
    )
    if pv.status_code != 201:
        raise SmokeError(
            STEP_SEED_ADMIN, f"create prompt_version failed: {pv.status_code} {pv.text}"
        )
    prompt_version_id = pv.json()["id"]

    roster: list[str] = []
    for slot in range(mini7_v1.PLAYER_COUNT):
        ab = await client.post(
            "/agent-builds",
            json={
                "display_name": "smoke-build",
                "model_config_id": mc_id,
                "prompt_version_id": prompt_version_id,
                "adapter_version": "smoke/0.1",
                "inference_params": {},
            },
            headers=headers,
        )
        if ab.status_code != 201:
            raise SmokeError(
                STEP_SEED_ADMIN,
                f"create agent_build #{slot} failed: {ab.status_code} {ab.text}",
            )
        roster.append(ab.json()["id"])

    lg = await client.post(
        "/leagues",
        json={
            "name": f"Smoke League {uuid.uuid4().hex[:6]}",
            "ruleset_id": mini7_v1.RULESET_ID,
            "ranked": True,
        },
        headers=headers,
    )
    if lg.status_code != 201:
        raise SmokeError(STEP_SEED_ADMIN, f"create league failed: {lg.status_code} {lg.text}")
    league_id = lg.json()["id"]

    # Generate Ed25519 signer and register a submitter key for the smoke test
    signer = Ed25519Signer.generate()
    pubkey_b64 = signer.public_key_b64()
    ab_resp = await client.post(
        "/admin/keys",
        json={
            "label": "smoke-submitter",
            "scopes": ["submitter"],
            "submission_public_key": pubkey_b64,
        },
        headers=headers,
    )
    if ab_resp.status_code != 201:
        raise SmokeError(
            STEP_SEED_ADMIN,
            f"create submitter key failed: {ab_resp.status_code} {ab_resp.text}",
        )
    submitter_raw_key = ab_resp.json()["raw_key"]
    ctx.signer = signer
    ctx.submitter_raw_key = submitter_raw_key

    return league_id, prompt_version_id, roster


async def _submit_gauntlet(
    client: AsyncClient,
    *,
    headers: dict[str, str],
    league_id: str,
    prompt_version_id: str,
    roster: list[str],
    clone_count: int,
) -> str:
    body = {
        "league_id": league_id,
        "ruleset_id": mini7_v1.RULESET_ID,
        "prompt_version_id": prompt_version_id,
        "clone_count": clone_count,
        "roster": roster,
    }
    resp = await client.post("/gauntlets", json=body, headers=headers)
    if resp.status_code != 202:
        raise SmokeError(STEP_SUBMIT_GAUNTLET, f"unexpected status {resp.status_code}: {resp.text}")
    return str(resp.json()["gauntlet_id"])


async def _poll_until_completed(
    client: AsyncClient,
    *,
    gauntlet_id: str,
    headers: dict[str, str],
    timeout_s: float,
    poll_interval_s: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_status: str | None = None
    while time.monotonic() < deadline:
        resp = await client.get(f"/gauntlets/{gauntlet_id}", headers=headers)
        if resp.status_code != 200:
            raise SmokeError(
                STEP_WAIT_COMPLETED,
                f"poll status {resp.status_code}: {resp.text}",
            )
        body: dict[str, Any] = resp.json()
        last_status = str(body.get("status"))
        if last_status == "COMPLETED":
            return body
        await asyncio.sleep(poll_interval_s)
    raise SmokeError(
        STEP_WAIT_COMPLETED,
        f"timeout after {timeout_s:.1f}s (last status={last_status!r})",
    )


async def _export_and_ingest_one_game(
    client: AsyncClient,
    *,
    ctx: _SmokeContext,
    headers: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
    gauntlet_body: dict[str, Any],
) -> str:
    games = gauntlet_body.get("games") or []
    completed_id: str | None = None
    for game in games:
        if game.get("status") == "COMPLETED":
            completed_id = str(game.get("id"))
            break
    if completed_id is None:
        raise SmokeError(
            STEP_EXPORT_INGEST,
            f"gauntlet reported COMPLETED but no child game has status=COMPLETED: {games!r}",
        )

    async with session_factory() as session:
        bundle = await export_game(session, uuid.UUID(completed_id), signer=ctx.signer)
    raw = bundle.model_dump_json()
    ingest_headers = (
        {"Authorization": f"Bearer {ctx.submitter_raw_key}"} if ctx.submitter_raw_key else headers
    )
    resp = await client.post(
        "/ingest/game",
        content=raw,
        headers={"Content-Type": "application/json", **ingest_headers},
    )
    if resp.status_code not in (200, 201):
        raise SmokeError(
            STEP_EXPORT_INGEST,
            f"ingest failed: {resp.status_code} {resp.text}",
        )
    body = resp.json()
    return str(body.get("game_id", completed_id))


def _require_keys(name: str, payload: dict[str, Any], required: frozenset[str]) -> None:
    missing = required - payload.keys()
    if missing:
        raise SmokeError(name, f"missing keys {sorted(missing)!r} in response: {payload!r}")


async def _assert_league_leaderboard(
    client: AsyncClient,
    *,
    headers: dict[str, str],
    league_id: str,
) -> dict[str, Any]:
    resp = await client.get(f"/leagues/{league_id}/leaderboard", headers=headers)
    if resp.status_code != 200:
        raise SmokeError(
            STEP_ASSERT_LEAGUE_LEADERBOARD,
            f"status {resp.status_code}: {resp.text}",
        )
    body = resp.json()
    _require_keys(STEP_ASSERT_LEAGUE_LEADERBOARD, body, _REQUIRED_LEAGUE_LEADERBOARD_KEYS)
    entries = body.get("entries") or []
    if not entries:
        raise SmokeError(STEP_ASSERT_LEAGUE_LEADERBOARD, "no entries returned")
    _require_keys(STEP_ASSERT_LEAGUE_LEADERBOARD, entries[0], _REQUIRED_LEAGUE_ENTRY_KEYS)
    return {"entries": len(entries), "first": entries[0]}


async def _assert_public_leaderboard(
    client: AsyncClient,
    *,
    headers: dict[str, str],
) -> dict[str, Any]:
    resp = await client.get(
        "/public/leaderboard",
        params={"ruleset_id": mini7_v1.RULESET_ID},
        headers=headers,
    )
    if resp.status_code != 200:
        raise SmokeError(
            STEP_ASSERT_PUBLIC_LEADERBOARD,
            f"status {resp.status_code}: {resp.text}",
        )
    body = resp.json()
    _require_keys(STEP_ASSERT_PUBLIC_LEADERBOARD, body, _REQUIRED_PUBLIC_LEADERBOARD_KEYS)
    entries = body.get("entries") or []
    if not entries:
        raise SmokeError(STEP_ASSERT_PUBLIC_LEADERBOARD, "no entries returned")
    _require_keys(STEP_ASSERT_PUBLIC_LEADERBOARD, entries[0], _REQUIRED_PUBLIC_ENTRY_KEYS)
    return {"entries": len(entries), "first": entries[0]}


async def _assert_public_models_leaderboard(
    client: AsyncClient,
    *,
    headers: dict[str, str],
    league_id: str,
) -> dict[str, Any]:
    resp = await client.get(
        "/public/models/leaderboard",
        params={"ruleset_id": mini7_v1.RULESET_ID, "league_id": league_id},
        headers=headers,
    )
    if resp.status_code != 200:
        raise SmokeError(
            STEP_ASSERT_PUBLIC_MODELS,
            f"status {resp.status_code}: {resp.text}",
        )
    body = resp.json()
    _require_keys(STEP_ASSERT_PUBLIC_MODELS, body, _REQUIRED_MODELS_LEADERBOARD_KEYS)
    entries = body.get("entries") or []
    if not entries:
        raise SmokeError(STEP_ASSERT_PUBLIC_MODELS, "no entries returned")
    _require_keys(STEP_ASSERT_PUBLIC_MODELS, entries[0], _REQUIRED_MODELS_ENTRY_KEYS)
    return {"entries": len(entries), "first": entries[0]}


async def _assert_public_game_events(
    client: AsyncClient,
    *,
    headers: dict[str, str],
    game_id: str,
) -> dict[str, Any]:
    resp = await client.get(f"/public/games/{game_id}/events", headers=headers)
    if resp.status_code != 200:
        raise SmokeError(
            STEP_ASSERT_PUBLIC_EVENTS,
            f"status {resp.status_code}: {resp.text}",
        )
    body = resp.json()
    _require_keys(STEP_ASSERT_PUBLIC_EVENTS, body, _REQUIRED_EVENTS_KEYS)
    items = body.get("items") or []
    if not items:
        raise SmokeError(STEP_ASSERT_PUBLIC_EVENTS, "no events returned")
    _require_keys(STEP_ASSERT_PUBLIC_EVENTS, items[0], _REQUIRED_EVENT_ITEM_KEYS)
    return {"items": len(items), "first_event_type": items[0].get("event_type")}


async def _execute_smoke_flow(
    client: AsyncClient,
    *,
    ctx: _SmokeContext,
    session_factory: async_sessionmaker[AsyncSession],
    headers: dict[str, str],
    clone_count: int,
    health_timeout_s: float,
    gauntlet_timeout_s: float,
    health_poll_interval_s: float,
    gauntlet_poll_interval_s: float,
) -> None:
    """Run the shared smoke flow against an already-running API."""
    await _wait_for_health(
        client,
        timeout_s=health_timeout_s,
        poll_interval_s=health_poll_interval_s,
    )
    ctx.add(STEP_HEALTHZ, "ok")

    scheduler_body = await _wait_for_scheduler_health(
        client,
        timeout_s=health_timeout_s,
        poll_interval_s=health_poll_interval_s,
    )
    ctx.add(STEP_HEALTHZ_SCHEDULER, "ok", scheduler_status=scheduler_body.get("status"))

    league_id, prompt_version_id, roster = await _seed_admin_entities(
        client, ctx=ctx, headers=headers
    )
    ctx.league_id = league_id
    ctx.add(STEP_SEED_ADMIN, "ok", league_id=league_id, roster_size=len(roster))

    gauntlet_id = await _submit_gauntlet(
        client,
        headers=headers,
        league_id=league_id,
        prompt_version_id=prompt_version_id,
        roster=roster,
        clone_count=clone_count,
    )
    ctx.gauntlet_id = gauntlet_id
    ctx.add(STEP_SUBMIT_GAUNTLET, "ok", gauntlet_id=gauntlet_id, clone_count=clone_count)

    gauntlet_body = await _poll_until_completed(
        client,
        gauntlet_id=gauntlet_id,
        headers=headers,
        timeout_s=gauntlet_timeout_s,
        poll_interval_s=gauntlet_poll_interval_s,
    )
    ctx.add(
        STEP_WAIT_COMPLETED,
        "ok",
        completed_games=sum(
            1 for g in gauntlet_body.get("games", []) if g.get("status") == "COMPLETED"
        ),
    )

    ingested_id = await _export_and_ingest_one_game(
        client,
        ctx=ctx,
        headers=headers,
        session_factory=session_factory,
        gauntlet_body=gauntlet_body,
    )
    ctx.ingested_game_id = ingested_id
    ctx.add(STEP_EXPORT_INGEST, "ok", ingested_game_id=ingested_id)

    league_summary = await _assert_league_leaderboard(client, headers=headers, league_id=league_id)
    ctx.add(STEP_ASSERT_LEAGUE_LEADERBOARD, "ok", entries=league_summary["entries"])

    pub_summary = await _assert_public_leaderboard(client, headers=headers)
    ctx.add(STEP_ASSERT_PUBLIC_LEADERBOARD, "ok", entries=pub_summary["entries"])

    models_summary = await _assert_public_models_leaderboard(
        client, headers=headers, league_id=league_id
    )
    ctx.add(STEP_ASSERT_PUBLIC_MODELS, "ok", entries=models_summary["entries"])

    events_summary = await _assert_public_game_events(client, headers=headers, game_id=ingested_id)
    ctx.add(STEP_ASSERT_PUBLIC_EVENTS, "ok", events=events_summary["items"])


async def run_smoke_in_process(
    *,
    db_url: str,
    clone_count: int = 1,
    health_timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S,
    gauntlet_timeout_s: float = DEFAULT_GAUNTLET_TIMEOUT_S,
    health_poll_interval_s: float = DEFAULT_HEALTH_POLL_INTERVAL_S,
    gauntlet_poll_interval_s: float = DEFAULT_GAUNTLET_POLL_INTERVAL_S,
) -> SmokeResult:
    """Run the smoke flow with the API + scheduler in the current event loop.

    Used by the unit test and any caller that wants a self-contained,
    single-process check (no subprocesses, no real network).
    """
    ctx = _SmokeContext()
    boot = await bootstrap(db_url=db_url, with_admin_key=True)
    _add_bootstrap_step(ctx, boot)
    if not boot.succeeded:
        return SmokeResult(
            succeeded=False,
            steps=tuple(ctx.steps),
            failed_step=boot.failed_step,
            failure_message=boot.failure_message,
            admin_raw_key=boot.admin_raw_key,
        )
    ctx.admin_raw_key = boot.admin_raw_key

    engine = create_engine(db_url)
    session_factory = create_session_factory(engine)
    app = create_app(session_factory=session_factory)
    transport = ASGITransport(app=app)

    stop_event = asyncio.Event()
    scheduler_task = asyncio.create_task(
        run_scheduler(
            session_factory,
            concurrency=1,
            stop_event=stop_event,
        ),
        name="smoke-scheduler",
    )

    try:
        async with AsyncClient(transport=transport, base_url="http://smoke") as client:
            try:
                await _execute_smoke_flow(
                    client,
                    ctx=ctx,
                    session_factory=session_factory,
                    headers=_admin_headers(ctx.admin_raw_key),
                    clone_count=clone_count,
                    health_timeout_s=health_timeout_s,
                    gauntlet_timeout_s=gauntlet_timeout_s,
                    health_poll_interval_s=health_poll_interval_s,
                    gauntlet_poll_interval_s=gauntlet_poll_interval_s,
                )
            except SmokeError as exc:
                ctx.add(exc.step, "failed", message=exc.message)
                return SmokeResult(
                    succeeded=False,
                    steps=tuple(ctx.steps),
                    failed_step=exc.step,
                    failure_message=exc.message,
                    admin_raw_key=ctx.admin_raw_key,
                    league_id=ctx.league_id,
                    gauntlet_id=ctx.gauntlet_id,
                    ingested_game_id=ctx.ingested_game_id,
                )
    finally:
        stop_event.set()
        try:
            await asyncio.wait_for(scheduler_task, timeout=10.0)
        except (TimeoutError, asyncio.CancelledError):
            scheduler_task.cancel()
        await engine.dispose()

    return SmokeResult(
        succeeded=True,
        steps=tuple(ctx.steps),
        admin_raw_key=ctx.admin_raw_key,
        league_id=ctx.league_id,
        gauntlet_id=ctx.gauntlet_id,
        ingested_game_id=ctx.ingested_game_id,
    )


def _add_bootstrap_step(ctx: _SmokeContext, boot: BootstrapResult) -> None:
    if boot.succeeded:
        ctx.add(
            STEP_BOOTSTRAP,
            "ok",
            steps=[{"name": s.name, "status": s.status} for s in boot.steps],
            admin_key_minted=boot.admin_raw_key is not None,
        )
    else:
        ctx.add(
            STEP_BOOTSTRAP,
            "failed",
            failed_step=boot.failed_step,
            failure_message=boot.failure_message,
        )


def _allocate_free_port() -> int:
    """Return an unused TCP port on 127.0.0.1.

    There's an inherent race between sniffing a free port and the child
    process binding it, but for a release-gate smoke this is acceptable —
    the alternative (passing port=0 to uvicorn and parsing it back from
    stderr) is a lot of plumbing for vanishingly low collision odds.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _StderrTail:
    """Capture the last ``maxlen`` stderr lines from a subprocess.

    The read loop runs on a *daemon* thread so it can never block interpreter
    shutdown. Under ``--keep-running`` the API/scheduler children stay alive and
    keep their stderr write-end open, so the blocking ``readline()`` never sees
    EOF. On Linux, closing the parent's read fd does NOT interrupt an in-flight
    ``read()`` on another thread — the worker stays blocked, and the
    ``ThreadPoolExecutor`` join that ``asyncio.to_thread`` registers at
    interpreter exit would then hang the smoke process forever (it never exits,
    so the dashboard-e2e ``padrino smoke localhost`` boot times out). A daemon
    thread is not joined at exit, so the process terminates cleanly regardless.
    macOS unblocks the read on ``close()``, which is why the smoke completed
    locally but hung for the full CI timeout on Linux.
    """

    def __init__(self, name: str, maxlen: int = 50) -> None:
        self.name = name
        self._buf: deque[str] = deque(maxlen=maxlen)
        self._thread: threading.Thread | None = None
        self._stream: Any = None

    def start(self, stream: Any) -> None:
        self._stream = stream

        def _pump() -> None:
            try:
                for raw in iter(stream.readline, b""):
                    text = raw.decode("utf-8", errors="replace").rstrip("\n")
                    self._buf.append(f"[{self.name}] {text}")
            except (OSError, ValueError):
                return

        self._thread = threading.Thread(target=_pump, name=f"smoke-stderr-{self.name}", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        # Best-effort: close the read fd so the daemon thread unblocks where the
        # OS supports it (macOS). On Linux the in-flight ``read()`` may stay
        # blocked, but the thread is a daemon so interpreter shutdown never waits
        # for it. Closing ``stream`` directly would instead wait for the in-flight
        # read to finish, which deadlocks under ``--keep-running``.
        if self._stream is not None:
            try:
                fd = self._stream.fileno()
            except (OSError, ValueError):
                fd = None
            if fd is not None and fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._stream = None
        self._thread = None

    def lines(self) -> list[str]:
        return list(self._buf)


async def run_smoke_subprocess(
    *,
    db_url: str,
    port: int | None = None,
    keep_running: bool = False,
    clone_count: int = 1,
    boot_timeout_s: float = DEFAULT_BOOT_TIMEOUT_S,
    health_timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S,
    gauntlet_timeout_s: float = DEFAULT_GAUNTLET_TIMEOUT_S,
    health_poll_interval_s: float = DEFAULT_HEALTH_POLL_INTERVAL_S,
    gauntlet_poll_interval_s: float = DEFAULT_GAUNTLET_POLL_INTERVAL_S,
    python_executable: str | None = None,
) -> SmokeResult:
    """Run the smoke flow with ``padrino serve`` and ``padrino scheduler`` as children.

    ``port=None`` picks a free localhost port. ``keep_running=True`` returns
    after a successful run without tearing down the children so an operator
    can poke at the live instance.
    """
    ctx = _SmokeContext()
    boot = await bootstrap(db_url=db_url, with_admin_key=True)
    _add_bootstrap_step(ctx, boot)
    if not boot.succeeded:
        return SmokeResult(
            succeeded=False,
            steps=tuple(ctx.steps),
            failed_step=boot.failed_step,
            failure_message=boot.failure_message,
            admin_raw_key=boot.admin_raw_key,
        )
    ctx.admin_raw_key = boot.admin_raw_key

    chosen_port = port if port is not None else _allocate_free_port()
    py = python_executable or sys.executable

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["PADRINO_DB_URL"] = db_url

    serve_cmd = [
        py,
        "-m",
        "padrino.cli",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(chosen_port),
        "--db-url",
        db_url,
    ]
    scheduler_cmd = [
        py,
        "-m",
        "padrino.cli",
        "scheduler",
        "--db-url",
        db_url,
    ]

    api_proc: subprocess.Popen[bytes] | None = None
    scheduler_proc: subprocess.Popen[bytes] | None = None
    api_tail = _StderrTail("api")
    scheduler_tail = _StderrTail("scheduler")

    try:
        api_proc = subprocess.Popen(
            serve_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        scheduler_proc = subprocess.Popen(
            scheduler_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        api_tail.start(api_proc.stderr)
        scheduler_tail.start(scheduler_proc.stderr)

        engine = create_engine(db_url)
        session_factory = create_session_factory(engine)
        base_url = f"http://127.0.0.1:{chosen_port}"

        try:
            async with AsyncClient(base_url=base_url, timeout=10.0) as client:
                try:
                    await _execute_smoke_flow(
                        client,
                        ctx=ctx,
                        session_factory=session_factory,
                        headers=_admin_headers(ctx.admin_raw_key),
                        clone_count=clone_count,
                        health_timeout_s=max(health_timeout_s, boot_timeout_s),
                        gauntlet_timeout_s=gauntlet_timeout_s,
                        health_poll_interval_s=health_poll_interval_s,
                        gauntlet_poll_interval_s=gauntlet_poll_interval_s,
                    )
                except SmokeError as exc:
                    ctx.add(exc.step, "failed", message=exc.message)
                    ctx.stderr_tail = api_tail.lines() + scheduler_tail.lines()
                    return SmokeResult(
                        succeeded=False,
                        steps=tuple(ctx.steps),
                        failed_step=exc.step,
                        failure_message=exc.message,
                        admin_raw_key=ctx.admin_raw_key,
                        league_id=ctx.league_id,
                        gauntlet_id=ctx.gauntlet_id,
                        ingested_game_id=ctx.ingested_game_id,
                        stderr_tail=ctx.stderr_tail,
                    )
        finally:
            await engine.dispose()
    finally:
        await api_tail.stop()
        await scheduler_tail.stop()
        if not keep_running:
            for proc in (scheduler_proc, api_proc):
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5.0)

    return SmokeResult(
        succeeded=True,
        steps=tuple(ctx.steps),
        admin_raw_key=ctx.admin_raw_key,
        league_id=ctx.league_id,
        gauntlet_id=ctx.gauntlet_id,
        ingested_game_id=ctx.ingested_game_id,
    )


__all__ = [
    "DEFAULT_BOOT_TIMEOUT_S",
    "DEFAULT_GAUNTLET_POLL_INTERVAL_S",
    "DEFAULT_GAUNTLET_TIMEOUT_S",
    "DEFAULT_HEALTH_POLL_INTERVAL_S",
    "DEFAULT_HEALTH_TIMEOUT_S",
    "STEP_ASSERT_LEAGUE_LEADERBOARD",
    "STEP_ASSERT_PUBLIC_EVENTS",
    "STEP_ASSERT_PUBLIC_LEADERBOARD",
    "STEP_ASSERT_PUBLIC_MODELS",
    "STEP_BOOTSTRAP",
    "STEP_EXPORT_INGEST",
    "STEP_HEALTHZ",
    "STEP_HEALTHZ_SCHEDULER",
    "STEP_SEED_ADMIN",
    "STEP_SUBMIT_GAUNTLET",
    "STEP_WAIT_COMPLETED",
    "SmokeError",
    "SmokeResult",
    "SmokeStepReport",
    "run_smoke_in_process",
    "run_smoke_subprocess",
]
