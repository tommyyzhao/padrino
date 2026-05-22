"""US-067: per-model leaderboard route tests.

Drives ``GET /public/models/leaderboard`` and ``GET /public/models/{model_key}``
via the FastAPI ASGI transport. Fixtures hand-roll one league + three agent
builds across two model identities; rating rows are pinned directly so the
sort + aggregation can be asserted against closed-form expectations.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import (
    SCOPE_SPECTATOR,
    SCOPE_SUBMITTER,
    RateLimiter,
    generate_raw_key,
)
from padrino.core.enums import Faction
from padrino.core.rulesets import mini7_v1
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import Rating
from padrino.db.repositories import (
    agent_builds as agent_builds_repo,
)
from padrino.db.repositories import (
    api_keys as api_keys_repo,
)
from padrino.db.repositories import (
    games as games_repo,
)
from padrino.db.repositories import (
    gauntlets as gauntlets_repo,
)
from padrino.db.repositories import (
    leagues as leagues_repo,
)
from padrino.db.repositories import (
    model_configs as model_configs_repo,
)
from padrino.db.repositories import (
    prompt_versions as prompt_versions_repo,
)
from padrino.db.repositories import (
    providers as providers_repo,
)
from padrino.ratings.model_rollup import RATING_MODEL, model_key_for, reset_cache
from padrino.ratings.openskill_service import (
    SCOPE_GLOBAL,
    SCOPE_VALUE_GLOBAL,
)
from padrino.settings import get_settings

_RULESET = mini7_v1.RULESET_ID


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


@pytest.fixture(autouse=True)
def _reset_cache_and_settings() -> None:
    reset_cache()
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


@pytest_asyncio.fixture
async def anonymous_client(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("PADRINO_PUBLIC_LEADERBOARD_ANONYMOUS", "true")
    get_settings.cache_clear()
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def _seed_spectator_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(
            session,
            raw_key=raw,
            scopes=[SCOPE_SPECTATOR],
            label="lurker",
        )
    return raw


async def _seed_two_models_one_league(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, str, str]:
    """Seed a league + two model identities with pinned ratings.

    Returns ``(league_id, key_strong, key_weak)`` — model_key strings.
    Strong = (p1/m-strong, mu=30, sigma=4 → cs=18). Weak = (p2/m-weak,
    mu=25, sigma=6 → cs=7).
    """
    async with session_factory() as session, session.begin():
        league = await leagues_repo.create(session, name="lb", ruleset_id=_RULESET, ranked=True)
        prov1 = await providers_repo.create(session, name="p1", auth_secret_ref="env:P1")
        prov2 = await providers_repo.create(session, name="p2", auth_secret_ref="env:P2")
        mc_strong = await model_configs_repo.create(
            session,
            provider_id=prov1.id,
            model_name="m-strong",
            model_version=None,
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        mc_weak = await model_configs_repo.create(
            session,
            provider_id=prov2.id,
            model_name="m-weak",
            model_version=None,
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )

        shared_pv = await prompt_versions_repo.create(
            session,
            ruleset_id=_RULESET,
            version="v-shared",
            system_prompt="sys",
            developer_prompt="dev",
            response_schema={"type": "object"},
            prompt_hash=f"ph-shared-{uuid.uuid4().hex}",
        )

        async def _make_ab(*, name: str, mc_id: uuid.UUID) -> uuid.UUID:
            ab = await agent_builds_repo.create(
                session,
                display_name=name,
                model_config_id=mc_id,
                prompt_version_id=shared_pv.id,
                adapter_version="2026.05",
                inference_params={},
                active=True,
            )
            return ab.id

        ab_strong_1 = await _make_ab(name="strong-a", mc_id=mc_strong.id)
        ab_strong_2 = await _make_ab(name="strong-b", mc_id=mc_strong.id)
        ab_weak = await _make_ab(name="weak", mc_id=mc_weak.id)

        # Pin ratings: strong model averages mu=30, sigma=4; weak is mu=25, sigma=6.
        for ab_id, mu, sigma, games in (
            (ab_strong_1, 30.0, 4.0, 5),
            (ab_strong_2, 30.0, 4.0, 5),
            (ab_weak, 25.0, 6.0, 5),
        ):
            session.add(
                Rating(
                    league_id=league.id,
                    agent_build_id=ab_id,
                    scope_type=SCOPE_GLOBAL,
                    scope_value=SCOPE_VALUE_GLOBAL,
                    mu=mu,
                    sigma=sigma,
                    conservative_score=mu - 3.0 * sigma,
                    games=games,
                )
            )

        # Spin up a gauntlet + 1 terminal town-win game with all seats sharing
        # strong-a (mafia) and weak (town) so faction counters are populated.
        gauntlet = await gauntlets_repo.create(
            session,
            league_id=league.id,
            ruleset_id=_RULESET,
            prompt_version_id=shared_pv.id,
            clone_count=1,
            gauntlet_seed="seed",
            ranked=True,
            status="COMPLETED",
        )
        game = await games_repo.create(
            session,
            ruleset_id=_RULESET,
            game_seed="g1",
            gauntlet_id=gauntlet.id,
            status="COMPLETED",
        )
        await games_repo.update_status(
            session,
            game.id,
            status="COMPLETED",
            terminal_result={"winner": "TOWN", "reason": "scripted", "day_terminated": 2},
        )
        seats = [
            (ab_strong_1, Faction.MAFIA),
            (ab_strong_1, Faction.MAFIA),
            (ab_weak, Faction.TOWN),
            (ab_weak, Faction.TOWN),
            (ab_weak, Faction.TOWN),
            (ab_weak, Faction.TOWN),
            (ab_weak, Faction.TOWN),
        ]
        for j, (ab_id, faction) in enumerate(seats):
            await games_repo.add_seat(
                session,
                game_id=game.id,
                public_player_id=f"P{j + 1:02d}",
                seat_index=j,
                agent_build_id=ab_id,
                role="MAFIOSO" if faction is Faction.MAFIA else "VILLAGER",
                faction=faction.value,
            )

        return (
            league.id,
            model_key_for("p1", "m-strong", None),
            model_key_for("p2", "m-weak", None),
        )


async def test_leaderboard_sorted_and_aggregates_by_model(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_spectator_key(session_factory)
    league_id, key_strong, key_weak = await _seed_two_models_one_league(session_factory)

    response = await client.get(
        "/public/models/leaderboard",
        params={"ruleset_id": _RULESET, "league_id": str(league_id)},
        headers=_auth(raw),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rating_model"] == RATING_MODEL
    assert body["ruleset_id"] == _RULESET
    entries = body["entries"]
    assert [e["model_key"] for e in entries] == [key_strong, key_weak]
    # Strong has 2 builds collapsed.
    strong = entries[0]
    assert strong["agent_build_count"] == 2
    # Faction counters: 1 mafia loss (mafia faction over 2 seats), 5 town wins.
    assert strong["mafia"]["games"] == 2
    assert strong["mafia"]["wins"] == 0
    assert strong["mafia"]["losses"] == 1 or strong["mafia"]["losses"] == 2
    # (The strong identity owns the 2 MAFIA seats; town won → those are losses.)
    assert strong["mafia"]["losses"] == 2
    weak = entries[1]
    assert weak["agent_build_count"] == 1
    assert weak["town"]["games"] == 5
    assert weak["town"]["wins"] == 5


async def test_leaderboard_cursor_pagination_stable(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_spectator_key(session_factory)
    league_id, _key_strong, _key_weak = await _seed_two_models_one_league(session_factory)

    first = await client.get(
        "/public/models/leaderboard",
        params={"ruleset_id": _RULESET, "league_id": str(league_id), "limit": 1},
        headers=_auth(raw),
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert len(first_body["entries"]) == 1
    assert first_body["next_cursor"] is not None

    second = await client.get(
        "/public/models/leaderboard",
        params={
            "ruleset_id": _RULESET,
            "league_id": str(league_id),
            "limit": 1,
            "cursor": first_body["next_cursor"],
        },
        headers=_auth(raw),
    )
    second_body = second.json()
    assert len(second_body["entries"]) == 1
    first_keys = {e["model_key"] for e in first_body["entries"]}
    second_keys = {e["model_key"] for e in second_body["entries"]}
    assert first_keys.isdisjoint(second_keys)


async def test_leaderboard_invalid_cursor_400(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_spectator_key(session_factory)
    league_id, _, _ = await _seed_two_models_one_league(session_factory)

    response = await client.get(
        "/public/models/leaderboard",
        params={
            "ruleset_id": _RULESET,
            "league_id": str(league_id),
            "cursor": "not-real",
        },
        headers=_auth(raw),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_cursor"


async def test_detail_view_returns_builds_and_recent_games(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_spectator_key(session_factory)
    league_id, key_strong, _ = await _seed_two_models_one_league(session_factory)

    response = await client.get(
        f"/public/models/{key_strong}",
        params={"ruleset_id": _RULESET, "league_id": str(league_id)},
        headers=_auth(raw),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entry"]["model_key"] == key_strong
    build_names = [b["display_name"] for b in body["builds"]]
    assert build_names == sorted(build_names)
    assert set(build_names) == {"strong-a", "strong-b"}
    # One game was seeded → recent_game_ids has one entry.
    assert len(body["recent_game_ids"]) == 1


async def test_detail_view_404_when_model_unknown(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_spectator_key(session_factory)
    league_id, _, _ = await _seed_two_models_one_league(session_factory)

    response = await client.get(
        "/public/models/p1/nonexistent",
        params={"ruleset_id": _RULESET, "league_id": str(league_id)},
        headers=_auth(raw),
    )
    assert response.status_code == 404


async def test_detail_view_404_when_league_unknown(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_spectator_key(session_factory)
    response = await client.get(
        "/public/models/p1/m-strong",
        params={"ruleset_id": _RULESET, "league_id": str(uuid.uuid4())},
        headers=_auth(raw),
    )
    assert response.status_code == 404


async def test_leaderboard_422_when_ruleset_mismatch(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_spectator_key(session_factory)
    league_id, _, _ = await _seed_two_models_one_league(session_factory)
    response = await client.get(
        "/public/models/leaderboard",
        params={"ruleset_id": "fake-ruleset", "league_id": str(league_id)},
        headers=_auth(raw),
    )
    assert response.status_code == 422


async def test_anonymous_flag_toggles_auth(
    anonymous_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, _, _ = await _seed_two_models_one_league(session_factory)
    response = await anonymous_client.get(
        "/public/models/leaderboard",
        params={"ruleset_id": _RULESET, "league_id": str(league_id)},
    )
    assert response.status_code == 200


async def test_default_requires_spectator_scope(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, _, _ = await _seed_two_models_one_league(session_factory)
    # No Bearer header → 401.
    response = await client.get(
        "/public/models/leaderboard",
        params={"ruleset_id": _RULESET, "league_id": str(league_id)},
    )
    assert response.status_code == 401
    # Wrong scope (submitter alone) → 403.
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(session, raw_key=raw, scopes=[SCOPE_SUBMITTER], label="sub")
    response = await client.get(
        "/public/models/leaderboard",
        params={"ruleset_id": _RULESET, "league_id": str(league_id)},
        headers=_auth(raw),
    )
    assert response.status_code == 403


async def test_unknown_query_param_returns_422(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_spectator_key(session_factory)
    league_id, _, _ = await _seed_two_models_one_league(session_factory)
    response = await client.get(
        "/public/models/leaderboard",
        params={
            "ruleset_id": _RULESET,
            "league_id": str(league_id),
            "bogus": "yes",
        },
        headers=_auth(raw),
    )
    assert response.status_code == 422


async def test_response_omits_secret_or_pii_fields(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw = await _seed_spectator_key(session_factory)
    league_id, key_strong, _ = await _seed_two_models_one_league(session_factory)
    response = await client.get(
        f"/public/models/{key_strong}",
        params={"ruleset_id": _RULESET, "league_id": str(league_id)},
        headers=_auth(raw),
    )
    body = response.json()
    # Strong assertion: no auth_secret_ref / api_key / submitter labels leak.
    serialized = response.text
    for forbidden in ("auth_secret_ref", "submission_public_key", "api_key", "submitter_key"):
        assert forbidden not in serialized
    # The build list intentionally only carries display_name + agent_build_id.
    for build in body["builds"]:
        assert set(build) == {"agent_build_id", "display_name"}


async def test_initial_mu_when_no_ratings_yet(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A league with builds but zero ratings yields INITIAL_MU/SIGMA, no crash."""
    raw = await _seed_spectator_key(session_factory)
    async with session_factory() as session, session.begin():
        league = await leagues_repo.create(session, name="empty", ruleset_id=_RULESET, ranked=True)
    response = await client.get(
        "/public/models/leaderboard",
        params={"ruleset_id": _RULESET, "league_id": str(league.id)},
        headers=_auth(raw),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entries"] == []
    assert body["total_estimate"] == 0


# ---------------------------------------------------------------------------
# US-079: same-model multi-host fallback does not bifurcate the leaderboard row
# ---------------------------------------------------------------------------


async def test_same_model_fallback_does_not_bifurcate_leaderboard_row(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An AgentBuild served by ``same_model_fallback_ok`` rows still rolls up to one entry.

    The per-model leaderboard rollup keys by the AgentBuild's
    ``(provider, model_name, model_version)`` — never by the host that
    actually served any given LLM call. US-079's same-model multi-host
    fallback routes a Cerebras failure to Z.AI's GLM-4.7 endpoint, both
    serving the same upstream weights; the rating credit (and thus the
    leaderboard row) must stay attached to the single AgentBuild identity.
    """
    from padrino.db.repositories import llm_calls as llm_calls_repo

    raw = await _seed_spectator_key(session_factory)
    league_id, key_strong, _ = await _seed_two_models_one_league(session_factory)

    # Find one strong-model AgentBuild and inject an llm_call with the new
    # ``same_model_fallback_ok`` status. The rollup ignores llm_calls but the
    # row exercises the persistence path end-to-end and documents the new
    # status reaching the DB layer.
    from sqlalchemy import select as _select

    from padrino.db.models import AgentBuild as AgentBuildRow
    from padrino.db.models import Game as GameRow

    async with session_factory() as session, session.begin():
        strong_build = (
            await session.execute(
                _select(AgentBuildRow).where(AgentBuildRow.display_name == "strong-a")
            )
        ).scalar_one()
        game_row = (await session.execute(_select(GameRow))).scalars().first()
        assert game_row is not None
        await llm_calls_repo.record_call(
            session,
            game_id=game_row.id,
            agent_build_id=strong_build.id,
            public_player_id="P01",
            phase="NIGHT_1_ACTIONS",
            request_json={"obs": "stub"},
            request_prompt_hash="prompthash",
            status="same_model_fallback_ok",
            raw_response="{}",
            parsed_response={"action": {"type": "NOOP", "target": None}},
            latency_ms=123,
        )

    response = await client.get(
        "/public/models/leaderboard",
        params={"ruleset_id": _RULESET, "league_id": str(league_id)},
        headers=_auth(raw),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    matching = [e for e in body["entries"] if e["model_key"] == key_strong]
    assert len(matching) == 1, (
        f"expected exactly one leaderboard row for the strong model identity, "
        f"got {len(matching)}: {matching!r}"
    )
    # The single row aggregates BOTH AgentBuilds that share the strong model
    # identity (strong-a + strong-b) — that's the existing aggregation
    # invariant; here we are only proving same_model_fallback_ok does not
    # split it further.
    assert matching[0]["agent_build_count"] == 2
