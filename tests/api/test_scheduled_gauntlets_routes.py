"""Tests for scheduled-gauntlet admin + public routes (US-085)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import prompt_versions as prompt_versions_repo
from padrino.db.repositories import providers as providers_repo
from padrino.llm.prompts import CANONICAL_RESPONSE_SCHEMA, iter_canonical_prompts

_SEATS = [f"P{i + 1:02d}" for i in range(mini7_v1.PLAYER_COUNT)]


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _seed_roster(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, dict[str, str]]:
    async with session_factory() as session, session.begin():
        canonical: dict[str, Any] = {}
        for template in iter_canonical_prompts(mini7_v1.RULESET_ID):
            canonical[template.role_family.value] = await prompt_versions_repo.create(
                session,
                ruleset_id=template.ruleset_id,
                version=template.version,
                system_prompt=template.system_prompt,
                developer_prompt=template.role_family.value,
                response_schema=CANONICAL_RESPONSE_SCHEMA,
                prompt_hash=template.prompt_hash,
            )
        pv = canonical["VANILLA_TOWN"]
        league = await leagues_repo.create(
            session, name="Sched League", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        provider = await providers_repo.create(
            session, name="mockprov", auth_secret_ref="env:MOCK_KEY"
        )
        roster: dict[str, str] = {}
        for seat in _SEATS:
            mc = await model_configs_repo.create(
                session,
                provider_id=provider.id,
                model_name=f"mock/{seat}",
                litellm_model_id=f"mock/{seat}",
                default_temperature=0.7,
                default_top_p=1.0,
                default_max_output_tokens=512,
                supports_structured_outputs=True,
            )
            ab = await agent_builds_repo.create(
                session,
                display_name=f"Mock {seat}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="mock-v1",
                inference_params={},
                active=True,
            )
            roster[seat] = str(ab.id)
        return league.id, roster


def _create_body(league_id: uuid.UUID, roster: dict[str, str], **over: Any) -> dict[str, Any]:
    body = {
        "name": "nightly",
        "schedule_cron": "0 2 * * *",
        "roster_spec": {"league_id": str(league_id), "roster": roster},
        "n_games": 1,
        "cost_cap_usd": 1.0,
        "enabled": True,
    }
    body.update(over)
    return body


async def test_create_and_public_scrubbed_shape(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    league_id, roster = await _seed_roster(session_factory)
    resp = await client.post("/admin/scheduled-gauntlets", json=_create_body(league_id, roster))
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["next_run_at"] is not None

    pub = await client.get("/public/scheduled-gauntlets")
    assert pub.status_code == 200, pub.text
    payload = pub.json()
    assert len(payload["schedules"]) == 1
    entry = payload["schedules"][0]
    assert entry["name"] == "nightly"
    assert entry["schedule_cron_human"] == "every day at 02:00 UTC"
    assert entry["status"] == "scheduled"
    # Scrubbed: no raw cron, no cost cap anywhere in the public payload.
    flat = str(payload)
    assert "0 2 * * *" not in flat
    assert "cost_cap" not in flat
    assert "schedule_cron" not in entry  # only the humanized form is exposed


async def test_create_rejects_bad_cron(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    league_id, roster = await _seed_roster(session_factory)
    resp = await client.post(
        "/admin/scheduled-gauntlets",
        json=_create_body(league_id, roster, schedule_cron="99 * * * *"),
    )
    assert resp.status_code == 422
    assert "schedule_cron" in resp.text


async def test_create_rejects_unknown_agent_build(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    league_id, roster = await _seed_roster(session_factory)
    roster = dict(roster)
    roster["P01"] = str(uuid.uuid4())  # nonexistent
    resp = await client.post("/admin/scheduled-gauntlets", json=_create_body(league_id, roster))
    assert resp.status_code == 422
    assert "agent_build_id" in resp.text


async def test_create_rejects_duplicate_name(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    league_id, roster = await _seed_roster(session_factory)
    body = _create_body(league_id, roster)
    assert (await client.post("/admin/scheduled-gauntlets", json=body)).status_code == 201
    dup = await client.post("/admin/scheduled-gauntlets", json=body)
    assert dup.status_code == 409


async def test_delete_soft_disables_and_clears_next_run(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    league_id, roster = await _seed_roster(session_factory)
    created = (
        await client.post("/admin/scheduled-gauntlets", json=_create_body(league_id, roster))
    ).json()
    sid = created["id"]
    deleted = await client.delete(f"/admin/scheduled-gauntlets/{sid}")
    assert deleted.status_code == 200
    assert deleted.json()["enabled"] is False

    pub = (await client.get("/public/scheduled-gauntlets")).json()
    entry = pub["schedules"][0]
    assert entry["status"] == "disabled"
    assert entry["next_run_at"] is None


async def test_patch_disable_then_reenable_recomputes_next_run(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    league_id, roster = await _seed_roster(session_factory)
    sid = (
        await client.post("/admin/scheduled-gauntlets", json=_create_body(league_id, roster))
    ).json()["id"]

    disabled = await client.patch(f"/admin/scheduled-gauntlets/{sid}", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["next_run_at"] is None

    reenabled = await client.patch(
        f"/admin/scheduled-gauntlets/{sid}", json={"enabled": True, "schedule_cron": "*/30 * * * *"}
    )
    assert reenabled.status_code == 200
    assert reenabled.json()["enabled"] is True
    assert reenabled.json()["schedule_cron"] == "*/30 * * * *"
    assert reenabled.json()["next_run_at"] is not None


async def test_patch_unknown_returns_404(client: AsyncClient) -> None:
    resp = await client.patch(f"/admin/scheduled-gauntlets/{uuid.uuid4()}", json={"enabled": False})
    assert resp.status_code == 404
