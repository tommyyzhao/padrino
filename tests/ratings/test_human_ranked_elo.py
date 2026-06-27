"""US-234b: ranked Humans-Included ELO writes only human-rating siblings."""

from __future__ import annotations

import ast
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy import delete, event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import LogCapture

from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, LeagueKind, RatingContextKind, SeatKind
from padrino.core.rulesets import mini7_v1
from padrino.db.models import (
    GameSeat,
    HumanRating,
    HumanRatingEvent,
    League,
    Rating,
    RatingContext,
    RatingEvent,
)
from padrino.db.repositories import (
    agent_builds,
    games,
    human_principals,
    human_ratings,
    leagues,
    model_configs,
    prompt_versions,
    providers,
)
from padrino.db.repositories import rating_contexts as rating_contexts_repo
from padrino.ratings.human_openskill_service import (
    FirewallViolation,
    HumanGameResult,
    update_human_ratings_for_game,
)
from padrino.ratings.openskill_service import (
    INITIAL_MU,
    INITIAL_SIGMA,
    SCOPE_GLOBAL,
    SCOPE_VALUE_GLOBAL,
)
from padrino.ratings.provisional_and_decay import DEFAULT_PROVISIONAL_GAMES, is_provisional

_GAME_SEED = "seed-human-ranked-elo-001"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCIENTIFIC_RATING_SQL_RE = re.compile(r"\b(?:ratings|rating_events)\b", re.IGNORECASE)
_ScientificSnapshot = tuple[tuple[tuple[Any, ...], ...], tuple[tuple[Any, ...], ...]]


async def _seed_human_rating_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    league_kind: LeagueKind,
    league_ranked: bool,
    hash_prefix: str,
    human_factions: set[Faction] | None = None,
    takeover_factions: set[Faction] | None = None,
    ai_agent_build_ids: list[uuid.UUID] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    requested_human_factions = (
        {Faction.MAFIA, Faction.TOWN} if human_factions is None else human_factions
    )
    requested_takeover_factions = takeover_factions or set()
    ai_build_iter = iter(ai_agent_build_ids or [])
    human_seats: dict[str, Faction] = {}
    assigned_factions: set[Faction] = set()
    for seat in seats:
        if seat.faction in requested_human_factions and seat.faction not in assigned_factions:
            human_seats[seat.public_player_id] = seat.faction
            assigned_factions.add(seat.faction)

    async with session_factory() as session, session.begin():
        if league_kind is LeagueKind.HUMANS_INCLUDED:
            league = await leagues.get_or_create_humans_included(
                session, ruleset_id=mini7_v1.RULESET_ID, ranked=league_ranked
            )
        else:
            league = await leagues.create(
                session,
                name=f"scientific-{hash_prefix}",
                ruleset_id=mini7_v1.RULESET_ID,
                ranked=league_ranked,
            )
        game = await games.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status="COMPLETED",
        )
        principals: dict[str, uuid.UUID] = {}
        for seat in seats:
            principal_id: uuid.UUID | None = None
            agent_build_id: uuid.UUID | None = None
            if seat.public_player_id in human_seats:
                principal = await human_principals.create_principal(
                    session,
                    kind=human_principals.PRINCIPAL_KIND_GUEST,
                    display_name=None,
                )
                principal_id = principal.id
                principals[seat.public_player_id] = principal.id
            elif ai_agent_build_ids is not None:
                try:
                    agent_build_id = next(ai_build_iter)
                except StopIteration as exc:  # pragma: no cover - helper misuse.
                    raise AssertionError("not enough AI agent builds for seeded seats") from exc
            seat_kind = SeatKind.AI.value
            if principal_id is not None:
                seat_kind = (
                    SeatKind.AI_TAKEOVER.value
                    if human_seats[seat.public_player_id] in requested_takeover_factions
                    else SeatKind.HUMAN.value
                )
            session.add(
                GameSeat(
                    game_id=game.id,
                    public_player_id=seat.public_player_id,
                    seat_index=seat.seat_index,
                    agent_build_id=agent_build_id,
                    seat_kind=seat_kind,
                    occupant_principal_id=principal_id,
                    role=seat.role.value,
                    faction=seat.faction.value,
                    alive=True,
                )
            )
    return league.id, game.id, principals


def _principal_for_faction(principals: dict[str, uuid.UUID], faction: Faction) -> uuid.UUID:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    return next(
        principals[seat.public_player_id]
        for seat in seats
        if seat.faction is faction and seat.public_player_id in principals
    )


async def _seed_ai_build_ids(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    count: int,
    hash_prefix: str,
) -> list[uuid.UUID]:
    async with session_factory() as session, session.begin():
        provider = await providers.create(
            session,
            name=f"provider-{hash_prefix}",
            auth_secret_ref="env:CEREBRAS_API_KEY",
        )
        model_config = await model_configs.create(
            session,
            provider_id=provider.id,
            model_name=f"model-{hash_prefix}",
            litellm_model_id=f"mock/{hash_prefix}",
            model_version="v1",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        build_ids: list[uuid.UUID] = []
        for index in range(count):
            prompt = await prompt_versions.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                version=f"{hash_prefix}-prompt-{index}",
                system_prompt="sys",
                developer_prompt="dev",
                response_schema={"type": "object"},
                prompt_hash=f"{hash_prefix}-hash-{index}",
            )
            build = await agent_builds.create(
                session,
                display_name=f"{hash_prefix}-build-{index}",
                model_config_id=model_config.id,
                prompt_version_id=prompt.id,
                adapter_version="2026.06",
                inference_params={"temperature": 0.7},
                active=True,
            )
            build_ids.append(build.id)
    return build_ids


async def _seed_scientific_ladder_rows(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    agent_build_ids: list[uuid.UUID],
    hash_prefix: str,
    mu: float,
    sigma: float,
) -> None:
    async with session_factory() as session, session.begin():
        scientific_league = await leagues.create(
            session,
            name=f"scientific-{hash_prefix}",
            ruleset_id=mini7_v1.RULESET_ID,
            ranked=True,
            kind=LeagueKind.SCIENTIFIC,
        )
        context = await rating_contexts_repo.resolve_canonical_team_context(
            session,
            league_id=scientific_league.id,
            ruleset_id=mini7_v1.RULESET_ID,
        )
        assert context is not None
        audit_game = await games.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=f"scientific-{hash_prefix}",
            status="COMPLETED",
        )
        for index, agent_build_id in enumerate(agent_build_ids):
            session.add(
                Rating(
                    league_id=scientific_league.id,
                    ruleset_id=mini7_v1.RULESET_ID,
                    rating_context_id=context.id,
                    agent_build_id=agent_build_id,
                    scope_type=SCOPE_GLOBAL,
                    scope_value=SCOPE_VALUE_GLOBAL,
                    mu=mu,
                    sigma=sigma,
                    conservative_score=mu - 3.0 * sigma,
                    games=50 + index,
                    last_game_at=datetime(2026, 6, 1, tzinfo=UTC),
                )
            )
            session.add(
                RatingEvent(
                    league_id=scientific_league.id,
                    game_id=audit_game.id,
                    ruleset_id=mini7_v1.RULESET_ID,
                    rating_context_id=context.id,
                    game_seed=audit_game.game_seed,
                    team_outcome="TOWN",
                    agent_build_id=agent_build_id,
                    public_player_id=f"P{index + 1:02d}",
                    scope_type=SCOPE_GLOBAL,
                    scope_value=SCOPE_VALUE_GLOBAL,
                    before_mu=INITIAL_MU,
                    before_sigma=INITIAL_SIGMA,
                    after_mu=mu,
                    after_sigma=sigma,
                )
            )


async def _scientific_ladder_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> _ScientificSnapshot:
    async with session_factory() as session:
        rating_rows = (await session.execute(select(Rating))).scalars().all()
        event_rows = (await session.execute(select(RatingEvent))).scalars().all()
    ratings_snapshot = tuple(
        (
            str(row.id),
            str(row.league_id),
            str(row.agent_build_id),
            row.ruleset_id,
            str(row.rating_context_id) if row.rating_context_id is not None else None,
            row.scope_type,
            row.scope_value,
            float(row.mu),
            float(row.sigma),
            float(row.conservative_score),
            row.games,
            row.last_game_at,
            row.updated_at,
        )
        for row in sorted(rating_rows, key=lambda item: str(item.id))
    )
    events_snapshot = tuple(
        (
            str(row.id),
            str(row.league_id),
            str(row.game_id),
            str(row.agent_build_id),
            row.ruleset_id,
            str(row.rating_context_id) if row.rating_context_id is not None else None,
            row.game_seed,
            row.team_outcome,
            row.public_player_id,
            row.scope_type,
            row.scope_value,
            float(row.before_mu),
            float(row.before_sigma),
            float(row.after_mu),
            float(row.after_sigma),
            row.created_at,
        )
        for row in sorted(event_rows, key=lambda item: str(item.id))
    )
    return ratings_snapshot, events_snapshot


async def _rating_for_principal(
    session_factory: async_sessionmaker[AsyncSession],
    principal_id: uuid.UUID,
) -> HumanRating:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(HumanRating).where(HumanRating.human_player_id == str(principal_id))
            )
        ).scalar_one()
    return row


async def _event_for_principal(
    session_factory: async_sessionmaker[AsyncSession],
    principal_id: uuid.UUID,
) -> HumanRatingEvent:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(HumanRatingEvent).where(
                    HumanRatingEvent.human_player_id == str(principal_id)
                )
            )
        ).scalar_one()
    return row


async def test_ranked_humans_included_updates_only_human_global_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, principals = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="ranked-human",
    )

    async with session_factory() as session, session.begin():
        events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
        )

    assert len(events) == 2

    async with session_factory() as session:
        human_rows = (await session.execute(select(HumanRating))).scalars().all()
        human_events = (await session.execute(select(HumanRatingEvent))).scalars().all()
        scientific_rows = (await session.execute(select(Rating))).scalars().all()
        scientific_events = (await session.execute(select(RatingEvent))).scalars().all()
        humans_included_canonical_count = (
            await session.execute(
                select(func.count())
                .select_from(Rating)
                .join(League, League.id == Rating.league_id)
                .join(RatingContext, RatingContext.id == Rating.rating_context_id)
                .where(
                    League.kind == LeagueKind.HUMANS_INCLUDED.value,
                    RatingContext.kind == RatingContextKind.CANONICAL_TEAM.value,
                )
            )
        ).scalar_one()

    assert scientific_rows == []
    assert scientific_events == []
    assert humans_included_canonical_count == 0
    assert len(human_rows) == 2
    assert len(human_events) == 2
    assert {row.scope_type for row in human_rows} == {"GLOBAL"}
    assert {row.scope_value for row in human_rows} == {"global"}
    assert {event.scope_type for event in human_events} == {"GLOBAL"}
    assert {event.scope_value for event in human_events} == {"global"}
    assert {row.human_player_id for row in human_rows} == {
        str(principal_id) for principal_id in principals.values()
    }
    assert {event.public_player_id for event in human_events} == set(principals)

    seats = assign_roles(_GAME_SEED, mini7_v1)
    faction_by_principal = {
        str(principals[seat.public_player_id]): seat.faction
        for seat in seats
        if seat.public_player_id in principals
    }
    by_human = {row.human_player_id: row for row in human_rows}
    for human_id, faction in faction_by_principal.items():
        row = by_human[human_id]
        if faction is Faction.TOWN:
            assert row.mu > INITIAL_MU
        else:
            assert row.mu < INITIAL_MU
        assert row.sigma < INITIAL_SIGMA
        assert row.games == 1
        assert row.conservative_score == pytest.approx(row.mu - 3.0 * row.sigma)


async def test_scientific_ai_strength_is_ignored_and_scientific_tables_are_not_touched(
    session_factory: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
) -> None:
    strong_ai_build_ids = await _seed_ai_build_ids(
        session_factory,
        count=mini7_v1.PLAYER_COUNT,
        hash_prefix="strong-ai-sos",
    )
    weak_ai_build_ids = await _seed_ai_build_ids(
        session_factory,
        count=mini7_v1.PLAYER_COUNT,
        hash_prefix="weak-ai-sos",
    )
    strong_league_id, strong_game_id, strong_principals = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="strong-ai-human-game",
        human_factions={Faction.TOWN},
        ai_agent_build_ids=strong_ai_build_ids,
    )
    weak_league_id, weak_game_id, weak_principals = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="weak-ai-human-game",
        human_factions={Faction.TOWN},
        ai_agent_build_ids=weak_ai_build_ids,
    )
    await _seed_scientific_ladder_rows(
        session_factory,
        agent_build_ids=strong_ai_build_ids,
        hash_prefix="strong-ai-sos",
        mu=60.0,
        sigma=2.0,
    )
    await _seed_scientific_ladder_rows(
        session_factory,
        agent_build_ids=weak_ai_build_ids,
        hash_prefix="weak-ai-sos",
        mu=5.0,
        sigma=12.0,
    )

    before_scientific_rows = await _scientific_ladder_snapshot(session_factory)
    touched_scientific_sql: list[str] = []

    def _capture_scientific_rating_sql(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if _SCIENTIFIC_RATING_SQL_RE.search(statement):
            touched_scientific_sql.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture_scientific_rating_sql)
    try:
        async with session_factory() as session, session.begin():
            strong_events = await update_human_ratings_for_game(
                session,
                league_id=strong_league_id,
                ranked=True,
                rating_enabled=True,
                game_result=HumanGameResult(game_id=strong_game_id, winner="TOWN"),
                now=datetime(2026, 6, 27, tzinfo=UTC),
            )
        async with session_factory() as session, session.begin():
            weak_events = await update_human_ratings_for_game(
                session,
                league_id=weak_league_id,
                ranked=True,
                rating_enabled=True,
                game_result=HumanGameResult(game_id=weak_game_id, winner="TOWN"),
                now=datetime(2026, 6, 27, tzinfo=UTC),
            )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture_scientific_rating_sql)

    after_scientific_rows = await _scientific_ladder_snapshot(session_factory)
    assert touched_scientific_sql == []
    assert after_scientific_rows == before_scientific_rows
    assert len(strong_events) == 1
    assert len(weak_events) == 1

    strong_event = strong_events[0]
    weak_event = weak_events[0]
    assert strong_event.public_player_id == weak_event.public_player_id
    assert strong_event.before_mu == pytest.approx(INITIAL_MU)
    assert weak_event.before_mu == pytest.approx(INITIAL_MU)
    assert strong_event.before_sigma == pytest.approx(INITIAL_SIGMA)
    assert weak_event.before_sigma == pytest.approx(INITIAL_SIGMA)
    assert strong_event.after_mu == pytest.approx(weak_event.after_mu)
    assert strong_event.after_sigma == pytest.approx(weak_event.after_sigma)

    strong_human_id = _principal_for_faction(strong_principals, Faction.TOWN)
    weak_human_id = _principal_for_faction(weak_principals, Faction.TOWN)
    strong_rating = await _rating_for_principal(session_factory, strong_human_id)
    weak_rating = await _rating_for_principal(session_factory, weak_human_id)
    assert strong_rating.mu == pytest.approx(weak_rating.mu)
    assert strong_rating.sigma == pytest.approx(weak_rating.sigma)
    assert strong_rating.conservative_score == pytest.approx(weak_rating.conservative_score)


async def test_ai_takeover_winning_faction_is_forfeit_loss_not_win_credit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, principals = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="takeover-winner-forfeit",
        takeover_factions={Faction.TOWN},
    )
    town_principal_id = _principal_for_faction(principals, Faction.TOWN)

    async with session_factory() as session, session.begin():
        events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=datetime(2026, 6, 27, tzinfo=UTC),
        )

    assert {event.human_player_id for event in events} == {
        str(principal_id) for principal_id in principals.values()
    }
    town_rating = await _rating_for_principal(session_factory, town_principal_id)
    town_event = await _event_for_principal(session_factory, town_principal_id)
    assert town_rating.mu < INITIAL_MU
    assert town_rating.conservative_score < INITIAL_MU - 3.0 * INITIAL_SIGMA
    assert town_event.before_mu == pytest.approx(INITIAL_MU)
    assert town_event.after_mu == pytest.approx(town_rating.mu)
    assert town_event.after_mu < town_event.before_mu


async def test_ai_takeover_losing_faction_is_still_one_loss(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, principals = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="takeover-loser-forfeit",
        takeover_factions={Faction.TOWN},
    )
    town_principal_id = _principal_for_faction(principals, Faction.TOWN)

    async with session_factory() as session, session.begin():
        await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="MAFIA"),
            now=datetime(2026, 6, 27, tzinfo=UTC),
        )

    town_rating = await _rating_for_principal(session_factory, town_principal_id)
    town_event = await _event_for_principal(session_factory, town_principal_id)
    assert town_rating.games == 1
    assert town_rating.mu < INITIAL_MU
    assert town_event.after_mu == pytest.approx(town_rating.mu)
    assert town_event.after_mu < town_event.before_mu


async def test_ai_takeover_forfeit_loss_counts_toward_provisional_threshold(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, principals = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="takeover-provisional-boundary",
        takeover_factions={Faction.TOWN},
    )
    town_principal_id = _principal_for_faction(principals, Faction.TOWN)
    assert is_provisional(DEFAULT_PROVISIONAL_GAMES - 1)

    async with session_factory() as session, session.begin():
        session.add(
            HumanRating(
                league_id=league_id,
                human_player_id=str(town_principal_id),
                scope_type=SCOPE_GLOBAL,
                scope_value=SCOPE_VALUE_GLOBAL,
                mu=INITIAL_MU,
                sigma=INITIAL_SIGMA,
                conservative_score=INITIAL_MU - 3.0 * INITIAL_SIGMA,
                games=DEFAULT_PROVISIONAL_GAMES - 1,
                last_game_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )

    async with session_factory() as session, session.begin():
        await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=datetime(2026, 6, 27, tzinfo=UTC),
        )

    town_rating = await _rating_for_principal(session_factory, town_principal_id)
    assert town_rating.games == DEFAULT_PROVISIONAL_GAMES
    assert is_provisional(town_rating.games) is False
    assert town_rating.mu < INITIAL_MU
    assert town_rating.conservative_score < INITIAL_MU - 3.0 * INITIAL_SIGMA


async def test_ai_takeover_forfeit_overrides_draw_freeze(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, principals = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="takeover-draw-forfeit",
        takeover_factions={Faction.TOWN},
    )
    town_principal_id = _principal_for_faction(principals, Faction.TOWN)

    async with session_factory() as session, session.begin():
        await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="DRAW"),
            now=datetime(2026, 6, 27, tzinfo=UTC),
        )

    town_rating = await _rating_for_principal(session_factory, town_principal_id)
    town_event = await _event_for_principal(session_factory, town_principal_id)
    assert town_rating.mu < INITIAL_MU
    assert town_event.after_mu == pytest.approx(town_rating.mu)
    assert town_event.after_mu < town_event.before_mu


async def test_human_seat_that_stays_human_is_rated_by_true_outcome(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, principals = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="human-winner-regression",
    )
    town_principal_id = _principal_for_faction(principals, Faction.TOWN)

    async with session_factory() as session, session.begin():
        await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=datetime(2026, 6, 27, tzinfo=UTC),
        )

    town_rating = await _rating_for_principal(session_factory, town_principal_id)
    assert town_rating.mu > INITIAL_MU
    assert town_rating.games == 1


async def test_ai_takeover_participates_as_transient_replacement_for_other_humans(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    takeover_league_id, takeover_game_id, takeover_principals = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="takeover-transient",
        takeover_factions={Faction.TOWN},
    )
    baseline_league_id, baseline_game_id, baseline_principals = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="ai-baseline",
        human_factions={Faction.MAFIA},
    )
    takeover_mafia_id = _principal_for_faction(takeover_principals, Faction.MAFIA)
    baseline_mafia_id = _principal_for_faction(baseline_principals, Faction.MAFIA)

    async with session_factory() as session, session.begin():
        await update_human_ratings_for_game(
            session,
            league_id=takeover_league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=takeover_game_id, winner="TOWN"),
            now=datetime(2026, 6, 27, tzinfo=UTC),
        )
    async with session_factory() as session, session.begin():
        await update_human_ratings_for_game(
            session,
            league_id=baseline_league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=baseline_game_id, winner="TOWN"),
            now=datetime(2026, 6, 27, tzinfo=UTC),
        )

    takeover_mafia = await _rating_for_principal(session_factory, takeover_mafia_id)
    baseline_mafia = await _rating_for_principal(session_factory, baseline_mafia_id)
    assert takeover_mafia.mu == pytest.approx(baseline_mafia.mu)
    assert takeover_mafia.sigma == pytest.approx(baseline_mafia.sigma)
    assert takeover_mafia.conservative_score == pytest.approx(baseline_mafia.conservative_score)


async def test_all_taken_over_human_seats_write_loss_events_without_win_credit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, principals = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="all-taken-over",
        takeover_factions={Faction.TOWN, Faction.MAFIA},
    )

    async with session_factory() as session, session.begin():
        events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=datetime(2026, 6, 27, tzinfo=UTC),
        )

    assert {event.human_player_id for event in events} == {
        str(principal_id) for principal_id in principals.values()
    }
    async with session_factory() as session:
        rows = (await session.execute(select(HumanRating))).scalars().all()
        human_events = (await session.execute(select(HumanRatingEvent))).scalars().all()
    assert len(rows) == len(principals)
    assert len(human_events) == len(principals)
    assert all(row.games == 1 for row in rows)
    assert all(row.mu < INITIAL_MU for row in rows)
    assert all(event.after_mu < event.before_mu for event in human_events)


async def test_deleted_principal_is_skipped_by_terminal_ranked_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, principals = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="deleted-human",
    )
    deleted_public_id, deleted_principal_id = next(iter(principals.items()))
    active_principal_ids = {
        principal_id
        for public_id, principal_id in principals.items()
        if public_id != deleted_public_id
    }

    async with session_factory() as session, session.begin():
        principal = await human_principals.get_principal(session, deleted_principal_id)
        assert principal is not None
        principal.deleted_at = datetime(2026, 6, 27, tzinfo=UTC)
        events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=datetime(2026, 6, 27, tzinfo=UTC),
        )

    assert {event.human_player_id for event in events} == {
        str(principal_id) for principal_id in active_principal_ids
    }

    async with session_factory() as session:
        human_rows = (await session.execute(select(HumanRating))).scalars().all()
        human_events = (await session.execute(select(HumanRatingEvent))).scalars().all()

    assert {row.human_player_id for row in human_rows} == {
        str(principal_id) for principal_id in active_principal_ids
    }
    assert {event.human_player_id for event in human_events} == {
        str(principal_id) for principal_id in active_principal_ids
    }
    assert str(deleted_principal_id) not in {row.human_player_id for row in human_rows}


async def test_human_writer_fails_closed_for_casual_runtime_unranked_and_missing_league(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    casual_league_id, casual_game_id, _ = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=False,
        hash_prefix="casual-human",
    )
    async with session_factory() as session, session.begin():
        casual_events = await update_human_ratings_for_game(
            session,
            league_id=casual_league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=casual_game_id, winner="TOWN"),
        )
        runtime_unranked_events = await update_human_ratings_for_game(
            session,
            league_id=casual_league_id,
            ranked=False,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=casual_game_id, winner="TOWN"),
        )
        missing_league_events = await update_human_ratings_for_game(
            session,
            league_id=uuid.uuid4(),
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=casual_game_id, winner="TOWN"),
        )

    assert casual_events == []
    assert runtime_unranked_events == []
    assert missing_league_events == []

    async with session_factory() as session:
        counts = (
            (await session.execute(select(func.count()).select_from(Rating))).scalar_one(),
            (await session.execute(select(func.count()).select_from(RatingEvent))).scalar_one(),
            (await session.execute(select(func.count()).select_from(HumanRating))).scalar_one(),
            (
                await session.execute(select(func.count()).select_from(HumanRatingEvent))
            ).scalar_one(),
        )

    assert counts == (0, 0, 0, 0)


async def test_human_writer_raises_firewall_violation_for_scientific_league_and_rolls_back(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scientific_league_id, scientific_game_id, _ = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.SCIENTIFIC,
        league_ranked=True,
        hash_prefix="scientific-firewall",
    )
    capture = LogCapture()
    original_config = structlog.get_config()
    structlog.reset_defaults()
    structlog.configure(processors=[capture], cache_logger_on_first_use=False)
    try:
        with pytest.raises(FirewallViolation) as raised:
            async with session_factory() as session, session.begin():
                await update_human_ratings_for_game(
                    session,
                    league_id=scientific_league_id,
                    ranked=True,
                    rating_enabled=True,
                    game_result=HumanGameResult(game_id=scientific_game_id, winner="TOWN"),
                )
    finally:
        structlog.reset_defaults()
        structlog.configure(**original_config)

    assert raised.value.league_id == scientific_league_id
    assert raised.value.league_kind == LeagueKind.SCIENTIFIC.value
    assert any(
        entry["event"] == "human.rating.firewall_violation"
        and entry["league_id"] == str(scientific_league_id)
        and entry["kind"] == LeagueKind.SCIENTIFIC.value
        for entry in capture.entries
    )
    async with session_factory() as session:
        counts = (
            (await session.execute(select(func.count()).select_from(Rating))).scalar_one(),
            (await session.execute(select(func.count()).select_from(RatingEvent))).scalar_one(),
            (await session.execute(select(func.count()).select_from(HumanRating))).scalar_one(),
            (
                await session.execute(select(func.count()).select_from(HumanRatingEvent))
            ).scalar_one(),
        )

    assert counts == (0, 0, 0, 0)


def test_human_writer_import_graph_avoids_scientific_rating_writers() -> None:
    source_path = _REPO_ROOT / "src" / "padrino" / "ratings" / "human_openskill_service.py"
    tree = ast.parse(source_path.read_text())
    allowed = {"INITIAL_MU", "INITIAL_SIGMA", "SCOPE_GLOBAL", "SCOPE_VALUE_GLOBAL"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "padrino.ratings.openskill_service":
            imported.update(alias.name for alias in node.names)

    assert imported
    assert imported <= allowed


def test_human_writer_docstring_records_no_ai_strength_of_schedule_decision() -> None:
    import padrino.ratings.human_openskill_service as human_openskill_service

    docstring = " ".join((human_openskill_service.__doc__ or "").lower().split())
    assert "no-strength-of-schedule" in docstring
    assert "scientific" in docstring
    assert "firewall" in docstring
    assert "future work" in docstring


async def test_human_writer_kill_switch_off_writes_nothing_for_ranked_league(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, _ = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="ranked-human-switch-off",
    )

    async with session_factory() as session, session.begin():
        events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=False,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
        )

    assert events == []
    async with session_factory() as session:
        human_rows = (await session.execute(select(HumanRating))).scalars().all()
        human_events = (await session.execute(select(HumanRatingEvent))).scalars().all()
    assert human_rows == []
    assert human_events == []


async def test_human_rating_update_is_idempotent_per_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, _ = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="ranked-human-idempotent",
    )
    first_now = datetime(2026, 6, 1, tzinfo=UTC)
    retry_now = datetime(2026, 6, 2, tzinfo=UTC)

    async with session_factory() as session, session.begin():
        first_events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=first_now,
        )
    assert len(first_events) == 2

    async with session_factory() as session:
        first_rows = (await session.execute(select(HumanRating))).scalars().all()
        first_snapshot = {
            row.human_player_id: (
                row.mu,
                row.sigma,
                row.conservative_score,
                row.games,
                row.last_game_at,
            )
            for row in first_rows
        }

    async with session_factory() as session, session.begin():
        duplicate_events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=retry_now,
        )

    assert duplicate_events == []
    async with session_factory() as session:
        rows = (await session.execute(select(HumanRating))).scalars().all()
        event_count = (
            await session.execute(select(func.count()).select_from(HumanRatingEvent))
        ).scalar_one()
    assert event_count == 2
    assert {
        row.human_player_id: (
            row.mu,
            row.sigma,
            row.conservative_score,
            row.games,
            row.last_game_at,
        )
        for row in rows
    } == first_snapshot


async def test_human_rating_partial_retry_completes_missing_seats_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, _ = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="ranked-human-partial-retry",
    )
    first_now = datetime(2026, 6, 1, tzinfo=UTC)
    retry_now = datetime(2026, 6, 2, tzinfo=UTC)

    async with session_factory() as session, session.begin():
        await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=first_now,
        )

    async with session_factory() as session:
        first_rows = (await session.execute(select(HumanRating))).scalars().all()
        first_snapshot = {
            row.human_player_id: (
                row.mu,
                row.sigma,
                row.conservative_score,
                row.games,
                row.last_game_at,
            )
            for row in first_rows
        }
        first_events = (await session.execute(select(HumanRatingEvent))).scalars().all()

    missing_event = first_events[0]
    missing_human_id = missing_event.human_player_id
    preserved_human_id = first_events[1].human_player_id

    async with session_factory() as session, session.begin():
        await session.execute(
            delete(HumanRatingEvent).where(HumanRatingEvent.human_player_id == missing_human_id)
        )
        missing_row = (
            await session.execute(
                select(HumanRating).where(HumanRating.human_player_id == missing_human_id)
            )
        ).scalar_one()
        missing_row.mu = INITIAL_MU
        missing_row.sigma = INITIAL_SIGMA
        missing_row.conservative_score = INITIAL_MU - 3.0 * INITIAL_SIGMA
        missing_row.games = 0
        missing_row.last_game_at = None

    async with session_factory() as session, session.begin():
        retry_events = await update_human_ratings_for_game(
            session,
            league_id=league_id,
            ranked=True,
            rating_enabled=True,
            game_result=HumanGameResult(game_id=game_id, winner="TOWN"),
            now=retry_now,
        )

    assert [event.human_player_id for event in retry_events] == [missing_human_id]
    async with session_factory() as session:
        rows = (await session.execute(select(HumanRating))).scalars().all()
        events = (await session.execute(select(HumanRatingEvent))).scalars().all()

    by_human = {row.human_player_id: row for row in rows}
    preserved = by_human[preserved_human_id]
    assert (
        preserved.mu,
        preserved.sigma,
        preserved.conservative_score,
        preserved.games,
        preserved.last_game_at,
    ) == first_snapshot[preserved_human_id]

    repaired = by_human[missing_human_id]
    expected_mu, expected_sigma, expected_score, expected_games, _expected_time = first_snapshot[
        missing_human_id
    ]
    assert repaired.mu == pytest.approx(expected_mu)
    assert repaired.sigma == pytest.approx(expected_sigma)
    assert repaired.conservative_score == pytest.approx(expected_score)
    assert repaired.games == expected_games
    assert repaired.last_game_at is not None
    assert len(events) == 2


async def test_record_human_rating_event_conflict_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, principals = await _seed_human_rating_game(
        session_factory,
        league_kind=LeagueKind.HUMANS_INCLUDED,
        league_ranked=True,
        hash_prefix="ranked-human-event-conflict",
    )
    public_player_id, principal_id = next(iter(principals.items()))

    async with session_factory() as session, session.begin():
        first = await human_ratings.record_human_rating_event(
            session,
            league_id=league_id,
            game_id=game_id,
            human_player_id=str(principal_id),
            public_player_id=public_player_id,
            scope_type="GLOBAL",
            scope_value="global",
            before_mu=INITIAL_MU,
            before_sigma=INITIAL_SIGMA,
            after_mu=INITIAL_MU + 1.0,
            after_sigma=INITIAL_SIGMA - 1.0,
        )
        duplicate = await human_ratings.record_human_rating_event(
            session,
            league_id=league_id,
            game_id=game_id,
            human_player_id=str(principal_id),
            public_player_id=public_player_id,
            scope_type="GLOBAL",
            scope_value="global",
            before_mu=INITIAL_MU,
            before_sigma=INITIAL_SIGMA,
            after_mu=INITIAL_MU + 1.0,
            after_sigma=INITIAL_SIGMA - 1.0,
        )

    assert first is not None
    assert duplicate is None
    async with session_factory() as session:
        event_count = (
            await session.execute(select(func.count()).select_from(HumanRatingEvent))
        ).scalar_one()
    assert event_count == 1
