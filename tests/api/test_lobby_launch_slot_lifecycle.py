"""US-198: admission slot lifecycle around launch (bind/release/no-double-count).

Round-2 review findings #2 + #3:

* The launch handler must capture its ``HumanAdmitDecision`` and bind the slots to
  the launched lobby/host member ONLY when a NEW game is materialized; an
  idempotent re-launch must leak no slots (two launches => exactly ONE live
  ``HumanCostAdmission`` game slot for the host).
* A created+launched single game must consume exactly ONE game-bucket count slot
  (no create+launch double-count) so a ``games_per_day=1`` host can still launch.
* A game's inference reservations are released once spend transitions to charged
  ``LlmCall`` accounting, so the held reservation and ``_implicit_budget_used``
  never count the same dollars twice — admission keeps succeeding until charged
  spend actually reaches the cap.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.db.models import (
    AgentBuild,
    Game,
    HumanCostAdmission,
    HumanInferenceReservation,
    LlmCall,
    ModelConfig,
    ModelProvider,
    PromptVersion,
)
from padrino.db.repositories import human_principals as principals_repo
from padrino.settings import Settings


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


@pytest_asyncio.fixture
async def app_and_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[object, AsyncClient]]:
    app = create_app(session_factory=session_factory, auth_required=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield app, ac


def _pin_settings(
    app: object,
    *,
    games_per_day: int = 50,
    joins_per_day: int = 50,
    inference_per_day: float = 1000.0,
    global_breaker: float = 1000.0,
    reserve_usd: float = 0.5,
) -> None:
    settings = Settings(
        padrino_human_max_games_per_user_per_day=games_per_day,
        padrino_human_max_joins_per_user_per_day=joins_per_day,
        padrino_human_max_inference_usd_per_user_per_day=inference_per_day,
        padrino_human_global_lobby_cost_breaker_usd=global_breaker,
        padrino_human_admission_inference_reserve_usd=reserve_usd,
    )
    app.state.auth_settings = settings  # type: ignore[attr-defined]


async def _guest_token(client: AsyncClient) -> str:
    resp = await client.post("/human/guest")
    assert resp.status_code == 201
    return resp.cookies[HUMAN_SESSION_COOKIE]


async def _principal_id_for_token(
    session_factory: async_sessionmaker[AsyncSession], token: str
) -> uuid.UUID:
    async with session_factory() as session:
        record = await principals_repo.get_session_by_token(session, token)
        assert record is not None
        return record.principal_id


async def _seed_curated_builds(session: AsyncSession, *, count: int) -> None:
    provider = ModelProvider(name="cerebras", base_url=None, auth_secret_ref="CEREBRAS_API_KEY")
    session.add(provider)
    await session.flush()
    mc = ModelConfig(
        provider_id=provider.id,
        model_name="zai-glm-4.7",
        model_version=None,
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=4096,
        supports_structured_outputs=True,
    )
    session.add(mc)
    pv = PromptVersion(
        ruleset_id="mini7_v1",
        version="v1",
        system_prompt="play",
        developer_prompt="json",
        response_schema={"type": "object"},
        prompt_hash="hash-us198",
    )
    session.add(pv)
    await session.flush()
    for i in range(count):
        session.add(
            AgentBuild(
                display_name=f"cerebras/glm-4.7@v1-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="2026.05",
                inference_params={"temperature": 0.7},
                active=True,
            )
        )
    await session.commit()


async def _create_and_lock(client: AsyncClient, token: str) -> uuid.UUID:
    created = await client.post(
        "/lobbies", json={"ruleset_id": "mini7_v1"}, cookies={HUMAN_SESSION_COOKIE: token}
    )
    assert created.status_code == 201, created.text
    lobby_id = uuid.UUID(created.json()["id"])
    locked = await client.post(f"/lobbies/{lobby_id}/lock", cookies={HUMAN_SESSION_COOKIE: token})
    assert locked.status_code == 200, locked.text
    return lobby_id


async def _live_game_admission_count(
    session_factory: async_sessionmaker[AsyncSession], principal_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(HumanCostAdmission)
                    .where(
                        HumanCostAdmission.principal_id == principal_id,
                        HumanCostAdmission.bucket == "game",
                        HumanCostAdmission.released_at.is_(None),
                    )
                )
            ).scalar_one()
        )


async def _live_inference_reservation_count(
    session_factory: async_sessionmaker[AsyncSession], lobby_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(HumanInferenceReservation)
                    .where(
                        HumanInferenceReservation.lobby_id == lobby_id,
                        HumanInferenceReservation.released_at.is_(None),
                    )
                )
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# AC #1: idempotent re-launch leaks no slots (exactly ONE live game slot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_launch_leaves_exactly_one_live_game_slot(
    app_and_client: tuple[object, AsyncClient],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app, client = app_and_client
    _pin_settings(app, games_per_day=50)
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    token = await _guest_token(client)
    principal_id = await _principal_id_for_token(session_factory, token)
    lobby_id = await _create_and_lock(client, token)

    first = await client.post(f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: token})
    assert first.status_code == 200, first.text
    assert first.json()["created"] is True
    game_id = first.json()["game_id"]

    second = await client.post(f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: token})
    assert second.status_code == 200, second.text
    assert second.json()["created"] is False
    assert second.json()["game_id"] == game_id

    # The idempotent re-launch claimed nothing: exactly ONE live game count slot.
    assert await _live_game_admission_count(session_factory, principal_id) == 1
    # Exactly one game materialized (the re-launch wrote nothing).
    async with session_factory() as session:
        games = (await session.execute(select(func.count()).select_from(Game))).scalar_one()
    assert games == 1


# ---------------------------------------------------------------------------
# AC #2: a created+launched single game consumes exactly ONE game count slot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_then_launch_no_double_count_at_games_per_day_one(
    app_and_client: tuple[object, AsyncClient],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app, client = app_and_client
    _pin_settings(app, games_per_day=1)
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    token = await _guest_token(client)
    principal_id = await _principal_id_for_token(session_factory, token)

    # create consumes the single game slot; launch of the SAME lobby must NOT be
    # blocked by the host's own create slot (no create+launch double-count).
    lobby_id = await _create_and_lock(client, token)
    launched = await client.post(
        f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: token}
    )
    assert launched.status_code == 200, launched.text
    assert launched.json()["created"] is True

    # Exactly one live game count slot remains for the launched game.
    assert await _live_game_admission_count(session_factory, principal_id) == 1


@pytest.mark.asyncio
async def test_second_game_denied_after_create_launch_at_cap_one(
    app_and_client: tuple[object, AsyncClient],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After the day's single game is created+launched, a new create is denied."""
    app, client = app_and_client
    _pin_settings(app, games_per_day=1)
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    token = await _guest_token(client)
    lobby_id = await _create_and_lock(client, token)
    launched = await client.post(
        f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: token}
    )
    assert launched.status_code == 200, launched.text

    denied = await client.post(
        "/lobbies", json={"ruleset_id": "mini7_v1"}, cookies={HUMAN_SESSION_COOKIE: token}
    )
    assert denied.status_code == 429, denied.text
    assert denied.json()["detail"] == "daily_game_cap_reached"


# ---------------------------------------------------------------------------
# AC #3: launch releases held inference reservations; charged spend is the truth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launch_releases_inference_reservations(
    app_and_client: tuple[object, AsyncClient],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app, client = app_and_client
    _pin_settings(app, games_per_day=50, inference_per_day=10.0, reserve_usd=0.5)
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    token = await _guest_token(client)
    lobby_id = await _create_and_lock(client, token)
    launched = await client.post(
        f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: token}
    )
    assert launched.status_code == 200, launched.text
    # No held inference reservation survives a launch: charged spend now governs
    # the inference-$ cap.
    assert await _live_inference_reservation_count(session_factory, lobby_id) == 0


@pytest.mark.asyncio
async def test_held_reservations_do_not_double_count_charged_spend(
    app_and_client: tuple[object, AsyncClient],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """N created+launched games with real charged spend below the $ cap keep
    admitting (held reservation + charged spend never count the same dollars).

    With reserve=0.5 and an inference cap of 5.0 (10 slots), each game charges
    real $1.0 (2 slots). If launch did NOT release its held reservations, each
    game would burn the held reservation slots AND the charged slots (double
    count), denying the user at ~half the real $ cap. With the release, only the
    charged spend counts, so 4 games ($4.0) stay under the $5.0 cap and a 5th
    create still admits — until charged spend actually reaches the cap.
    """
    app, client = app_and_client
    _pin_settings(
        app, games_per_day=50, inference_per_day=5.0, global_breaker=1000.0, reserve_usd=0.5
    )
    async with session_factory() as session:
        await _seed_curated_builds(session, count=6)
    token = await _guest_token(client)
    principal_id = await _principal_id_for_token(session_factory, token)

    # Launch 4 games, each charging $1.0 of real spend attributed to the host.
    for _ in range(4):
        lobby_id = await _create_and_lock(client, token)
        launched = await client.post(
            f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: token}
        )
        assert launched.status_code == 200, launched.text
        game_id = uuid.UUID(launched.json()["game_id"])
        async with session_factory() as session, session.begin():
            session.add(
                LlmCall(
                    game_id=game_id,
                    public_player_id="P01",
                    phase="DAY_DISCUSSION",
                    request_json={},
                    request_prompt_hash="hash",
                    status="ok",
                    cost_usd=1.0,
                )
            )

    # Charged spend is $4.0 against a $5.0 cap. Without the release fix the held
    # reservations would have already tripped daily_inference_cap_reached; with
    # it, a new create still admits.
    ok = await client.post(
        "/lobbies", json={"ruleset_id": "mini7_v1"}, cookies={HUMAN_SESSION_COOKIE: token}
    )
    assert ok.status_code == 201, ok.text

    # Push charged spend to the cap and confirm admission now denies.
    new_lobby_id = uuid.UUID(ok.json()["id"])
    locked = await client.post(
        f"/lobbies/{new_lobby_id}/lock", cookies={HUMAN_SESSION_COOKIE: token}
    )
    assert locked.status_code == 200
    launched = await client.post(
        f"/lobbies/{new_lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: token}
    )
    assert launched.status_code == 200, launched.text
    game_id = uuid.UUID(launched.json()["game_id"])
    async with session_factory() as session, session.begin():
        session.add(
            LlmCall(
                game_id=game_id,
                public_player_id="P01",
                phase="DAY_DISCUSSION",
                request_json={},
                request_prompt_hash="hash",
                status="ok",
                cost_usd=1.0,
            )
        )
    # Charged spend is now $5.0 == cap: a new create is denied.
    denied = await client.post(
        "/lobbies", json={"ruleset_id": "mini7_v1"}, cookies={HUMAN_SESSION_COOKIE: token}
    )
    assert denied.status_code == 429, denied.text
    assert denied.json()["detail"] == "daily_inference_cap_reached"
    assert principal_id is not None  # spend attributed to the host principal
