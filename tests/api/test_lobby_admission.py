"""US-151: cost-governance admission ENFORCED at the real lobby endpoints.

These tests drive the actual ``POST /lobbies`` (create), ``POST /lobbies/join/{token}``
(join), and ``POST /lobbies/{id}/launch`` (launch) handlers — not the governance
library in isolation — and prove that:

* admission DENIES (429) once the calling principal is past a per-user/day cap,
* admission DENIES (429) once the global cost breaker is open,
* admission ALLOWS the action when under every cap (the wiring does not break the
  happy path),
* the denial is keyed on the calling principal (one principal's spend never
  blocks another).

Caps are pinned per-test by overriding ``app.state.auth_settings`` with a
``Settings`` carrying tiny thresholds, so the assertions are deterministic.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.db.models import Game, GameSeat, LlmCall
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
) -> None:
    """Override the app's admission settings with deterministic caps for a test."""
    settings = Settings(
        padrino_human_max_games_per_user_per_day=games_per_day,
        padrino_human_max_joins_per_user_per_day=joins_per_day,
        padrino_human_max_inference_usd_per_user_per_day=inference_per_day,
        padrino_human_global_lobby_cost_breaker_usd=global_breaker,
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


# ---------------------------------------------------------------------------
# create_lobby admission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_admitted_under_caps(
    app_and_client: tuple[object, AsyncClient],
) -> None:
    """Under every cap, create succeeds — the wiring does not break the happy path."""
    app, client = app_and_client
    _pin_settings(app, games_per_day=10)
    token = await _guest_token(client)
    resp = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_create_denied_when_global_breaker_open(
    app_and_client: tuple[object, AsyncClient],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An open global cost breaker denies a new lobby create with 429."""
    app, client = app_and_client
    _pin_settings(app, global_breaker=5.0)

    # Seed human-lane spend at/over the breaker threshold (a HUMAN-seat game).
    async with session_factory() as session, session.begin():
        game = Game(ruleset_id="mini7_v1", game_seed=f"seed-{uuid.uuid4()}", status="RUNNING")
        session.add(game)
        await session.flush()
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P00",
                seat_index=0,
                seat_kind="HUMAN",
                role="VILLAGER",
                faction="TOWN",
                alive=True,
            )
        )
        session.add(
            LlmCall(
                game_id=game.id,
                public_player_id="P01",
                phase="DAY_DISCUSSION",
                request_json={},
                request_prompt_hash="hash",
                status="ok",
                cost_usd=5.0,
            )
        )

    token = await _guest_token(client)
    resp = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"] == "breaker_open"


@pytest.mark.asyncio
async def test_create_denied_past_daily_game_cap(
    app_and_client: tuple[object, AsyncClient],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """At the per-user games/day cap, a new create is denied with 429."""
    app, client = app_and_client
    _pin_settings(app, games_per_day=1)
    token = await _guest_token(client)
    principal_id = await _principal_id_for_token(session_factory, token)

    # Attribute one game to the principal today (occupies a HUMAN seat).
    async with session_factory() as session, session.begin():
        game = Game(ruleset_id="mini7_v1", game_seed=f"seed-{uuid.uuid4()}", status="RUNNING")
        session.add(game)
        await session.flush()
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P00",
                seat_index=0,
                seat_kind="HUMAN",
                occupant_principal_id=principal_id,
                role="VILLAGER",
                faction="TOWN",
                alive=True,
            )
        )

    resp = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"] == "daily_game_cap_reached"


@pytest.mark.asyncio
async def test_create_cap_is_per_principal(
    app_and_client: tuple[object, AsyncClient],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One principal at its cap never blocks a DIFFERENT principal's create."""
    app, client = app_and_client
    _pin_settings(app, games_per_day=1)
    capped_token = await _guest_token(client)
    capped_id = await _principal_id_for_token(session_factory, capped_token)

    async with session_factory() as session, session.begin():
        game = Game(ruleset_id="mini7_v1", game_seed=f"seed-{uuid.uuid4()}", status="RUNNING")
        session.add(game)
        await session.flush()
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P00",
                seat_index=0,
                seat_kind="HUMAN",
                occupant_principal_id=capped_id,
                role="VILLAGER",
                faction="TOWN",
                alive=True,
            )
        )

    # The capped principal is denied ...
    denied = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1"},
        cookies={HUMAN_SESSION_COOKIE: capped_token},
    )
    assert denied.status_code == 429

    # ... but a fresh principal (no games today) is admitted.
    fresh_token = await _guest_token(client)
    ok = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1"},
        cookies={HUMAN_SESSION_COOKIE: fresh_token},
    )
    assert ok.status_code == 201, ok.text


# ---------------------------------------------------------------------------
# join_lobby admission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_denied_past_daily_join_cap(
    app_and_client: tuple[object, AsyncClient],
) -> None:
    """At the per-user joins/day cap, a NEW join is denied with 429."""
    app, client = app_and_client
    # Generous game cap so the host can create; a joins cap of 1.
    _pin_settings(app, games_per_day=50, joins_per_day=1)

    # Host creates two lobbies for the joiner to join.
    host_token = await _guest_token(client)
    first = await client.post(
        "/lobbies", json={"ruleset_id": "mini7_v1"}, cookies={HUMAN_SESSION_COOKIE: host_token}
    )
    second = await client.post(
        "/lobbies", json={"ruleset_id": "mini7_v1"}, cookies={HUMAN_SESSION_COOKIE: host_token}
    )
    first_token = first.json()["invite_token"]
    second_token = second.json()["invite_token"]

    joiner = await _guest_token(client)
    ok = await client.post(f"/lobbies/join/{first_token}", cookies={HUMAN_SESSION_COOKIE: joiner})
    assert ok.status_code == 200, ok.text

    denied = await client.post(
        f"/lobbies/join/{second_token}", cookies={HUMAN_SESSION_COOKIE: joiner}
    )
    assert denied.status_code == 429, denied.text
    assert denied.json()["detail"] == "daily_join_cap_reached"


@pytest.mark.asyncio
async def test_rejoin_is_exempt_from_join_cap(
    app_and_client: tuple[object, AsyncClient],
) -> None:
    """An idempotent re-join (already a member) consumes no slot and is allowed."""
    app, client = app_and_client
    _pin_settings(app, games_per_day=50, joins_per_day=1)
    host_token = await _guest_token(client)
    created = await client.post(
        "/lobbies", json={"ruleset_id": "mini7_v1"}, cookies={HUMAN_SESSION_COOKIE: host_token}
    )
    invite = created.json()["invite_token"]

    joiner = await _guest_token(client)
    first = await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: joiner})
    assert first.status_code == 200
    # Re-joining the SAME lobby is idempotent and still allowed at the cap.
    again = await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: joiner})
    assert again.status_code == 200, again.text


# ---------------------------------------------------------------------------
# launch admission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launch_denied_when_global_breaker_open(
    app_and_client: tuple[object, AsyncClient],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An open breaker denies launch with 429 BEFORE any game is materialized."""
    app, client = app_and_client
    # High caps so create/lock succeed; the breaker is what trips on launch.
    _pin_settings(app, games_per_day=50, global_breaker=1000.0)
    host_token = await _guest_token(client)
    created = await client.post(
        "/lobbies", json={"ruleset_id": "mini7_v1"}, cookies={HUMAN_SESSION_COOKIE: host_token}
    )
    lobby_id = created.json()["id"]
    lock = await client.post(
        f"/lobbies/{lobby_id}/lock", cookies={HUMAN_SESSION_COOKIE: host_token}
    )
    assert lock.status_code == 200, lock.text

    # Now open the breaker and lower its threshold so launch is denied.
    async with session_factory() as session, session.begin():
        game = Game(ruleset_id="mini7_v1", game_seed=f"seed-{uuid.uuid4()}", status="RUNNING")
        session.add(game)
        await session.flush()
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id="P00",
                seat_index=0,
                seat_kind="HUMAN",
                role="VILLAGER",
                faction="TOWN",
                alive=True,
            )
        )
        session.add(
            LlmCall(
                game_id=game.id,
                public_player_id="P01",
                phase="DAY_DISCUSSION",
                request_json={},
                request_prompt_hash="hash",
                status="ok",
                cost_usd=10.0,
            )
        )
    _pin_settings(app, games_per_day=50, global_breaker=5.0)

    resp = await client.post(
        f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: host_token}
    )
    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"] == "breaker_open"
