"""US-142: composition disclosure is counts-only and frozen across a takeover.

Every player / spectator / lobby surface that shows a game's composition must
disclose ONLY how many humans vs AI are present (decision 7) — never which seat
is which — and the counts must be frozen at game start so a silent AI takeover
(``HUMAN`` -> ``AI_TAKEOVER``) does not change them.

These tests cover the three composition consumers:

* the per-game spectator endpoint ``GET /public/games/{id}/composition``,
* the live index ``GET /public/live`` entry's ``composition`` field, and
* the single canonical producer
  :func:`padrino.public.composition_disclosure.composition_counts` that the
  lobby surface (US-147) consumes.

All assert counts-only (no per-seat human/AI map leaks) and frozen counts across
a takeover.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_SPECTATOR, RateLimiter, generate_raw_key
from padrino.db.models import Game, GameSeat
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.public.broadcast_index import BroadcastState
from padrino.public.composition_disclosure import composition_counts
from padrino.settings import get_settings

# Keys that, if they ever appeared in a composition response, would let a viewer
# reconstruct which seat is human vs AI. The disclosure must carry NONE of them.
_PER_SEAT_LEAK_KEYS = frozenset(
    {
        "seats",
        "seat_kind",
        "seat_kinds",
        "public_player_id",
        "occupant_principal_id",
        "is_human",
        "human_seats",
        "ai_seats",
        "seat_map",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_game(
    session: AsyncSession,
    *,
    broadcast_state: str = BroadcastState.LIVE.value,
    is_broadcastable: bool = True,
    status: str = "RUNNING",
) -> Game:
    g = Game(
        ruleset_id="mini7_v1",
        game_seed=f"seed-{uuid.uuid4()}",
        status=status,
        broadcast_state=broadcast_state,
        is_broadcastable=is_broadcastable,
    )
    session.add(g)
    await session.flush()
    return g


async def _add_seats(
    session: AsyncSession,
    game: Game,
    seat_kinds: list[str],
) -> None:
    """Seed seats of the given kinds. HUMAN seats carry no agent build."""
    for idx, kind in enumerate(seat_kinds):
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id=f"P{idx:02d}",
                seat_index=idx,
                agent_build_id=None,
                seat_kind=kind,
                role="VILLAGER",
                faction="TOWN",
                alive=True,
            )
        )
    await session.flush()


async def _seed_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    label: str,
) -> str:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(session, raw_key=raw, scopes=[SCOPE_SPECTATOR], label=label)
    return raw


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def _assert_no_per_seat_leak(payload: object) -> None:
    """Walk a JSON-able payload and assert no per-seat/human-AI map key appears."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert key not in _PER_SEAT_LEAK_KEYS, f"composition leaked per-seat key {key!r}"
            _assert_no_per_seat_leak(value)
    elif isinstance(payload, list | tuple):
        for item in payload:
            _assert_no_per_seat_leak(item)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Canonical producer (the lobby surface consumes this directly)
# ---------------------------------------------------------------------------


def test_composition_counts_is_counts_only() -> None:
    counts = composition_counts(["HUMAN", "HUMAN", "AI", "AI", "AI"])
    dumped = counts.model_dump()
    assert dumped == {"human_count": 2, "ai_count": 3, "total": 5}
    # Counts-only: there is no per-seat field on the model at all.
    assert set(dumped) == {"human_count", "ai_count", "total"}
    _assert_no_per_seat_leak(dumped)


def test_composition_counts_frozen_across_takeover() -> None:
    """A HUMAN seat taken over by an AI keeps the seat human in the counts."""
    before = composition_counts(["HUMAN", "HUMAN", "AI", "AI"])
    after = composition_counts(["AI_TAKEOVER", "HUMAN", "AI", "AI"])
    assert before == after
    assert after.model_dump() == {"human_count": 2, "ai_count": 2, "total": 4}


# ---------------------------------------------------------------------------
# Spectator surface: GET /public/games/{id}/composition
# ---------------------------------------------------------------------------


async def test_spectator_composition_endpoint_counts_only(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        await _add_seats(session, game, ["HUMAN", "HUMAN", "AI", "AI", "AI"])
        game_id = game.id

    r = await client.get(f"/public/games/{game_id}/composition", headers=_auth(raw))
    assert r.status_code == 200
    body = r.json()
    assert body["composition"] == {"human_count": 2, "ai_count": 3, "total": 5}
    _assert_no_per_seat_leak(body)


async def test_spectator_composition_endpoint_404_when_not_broadcastable(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game(session, broadcast_state=BroadcastState.HIDDEN.value)
        await _add_seats(session, game, ["HUMAN", "AI"])
        game_id = game.id

    r = await client.get(f"/public/games/{game_id}/composition", headers=_auth(raw))
    assert r.status_code == 404


async def test_spectator_composition_frozen_across_takeover(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Flipping a HUMAN seat to AI_TAKEOVER must not change the disclosed counts."""
    raw = await _seed_key(session_factory, label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game(session)
        await _add_seats(session, game, ["HUMAN", "HUMAN", "AI", "AI"])
        game_id = game.id

    r_before = await client.get(f"/public/games/{game_id}/composition", headers=_auth(raw))
    assert r_before.status_code == 200
    before = r_before.json()["composition"]

    # Silent AI takeover of one human seat (HUMAN -> AI_TAKEOVER).
    async with session_factory() as session, session.begin():
        await session.execute(
            update(GameSeat)
            .where(GameSeat.game_id == game_id, GameSeat.public_player_id == "P00")
            .values(seat_kind="AI_TAKEOVER", taken_over_at_phase="DAY_VOTE:1")
        )

    r_after = await client.get(f"/public/games/{game_id}/composition", headers=_auth(raw))
    assert r_after.status_code == 200
    after = r_after.json()["composition"]

    assert before == after == {"human_count": 2, "ai_count": 2, "total": 4}


# ---------------------------------------------------------------------------
# Live surface: GET /public/live entry composition field
# ---------------------------------------------------------------------------


async def test_live_index_entry_carries_counts_only_composition(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game(session, broadcast_state=BroadcastState.LIVE.value)
        await _add_seats(session, game, ["HUMAN", "AI", "AI"])
        game_id = game.id

    r = await client.get("/public/live", headers=_auth(raw))
    assert r.status_code == 200
    item = next(i for i in r.json()["items"] if i["game_id"] == str(game_id))
    assert item["composition"] == {"human_count": 1, "ai_count": 2, "total": 3}
    _assert_no_per_seat_leak(item)


async def test_live_index_composition_frozen_across_takeover(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_key(session_factory, label="viewer")
    async with session_factory() as session, session.begin():
        game = await _make_game(session, broadcast_state=BroadcastState.LIVE.value)
        await _add_seats(session, game, ["HUMAN", "HUMAN", "AI"])
        game_id = game.id

    r_before = await client.get("/public/live", headers=_auth(raw))
    before = next(i for i in r_before.json()["items"] if i["game_id"] == str(game_id))[
        "composition"
    ]

    async with session_factory() as session, session.begin():
        await session.execute(
            update(GameSeat)
            .where(GameSeat.game_id == game_id, GameSeat.public_player_id == "P01")
            .values(seat_kind="AI_TAKEOVER")
        )

    r_after = await client.get("/public/live", headers=_auth(raw))
    after = next(i for i in r_after.json()["items"] if i["game_id"] == str(game_id))["composition"]

    assert before == after == {"human_count": 2, "ai_count": 1, "total": 3}
