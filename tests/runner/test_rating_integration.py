"""US-039: rating updates wired to ``run_game``'s ``GameTerminated``.

Runs a scripted Town-win game with a ``GamePersistence`` carrying a
ranked-league ``league_id`` + per-seat ``agent_builds``, then asserts:

* Every Town seat's ``GLOBAL`` rating ``mu`` is above the initial mu.
* Every Mafia seat's ``GLOBAL`` rating ``mu`` is below the initial mu.
* Every Mafia + Town seat's ``FACTION`` rating row exists with the right
  scope_value and matching mu direction.
* A ``RatingEvent`` audit row was appended for every updated scope-row.
* When ``ranked=False`` (or ``league_id`` missing) the rating tables remain
  empty even though the rest of persistence still runs.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog, StoredEvent
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.engine.state import GameState, Phase
from padrino.core.enums import ActionType, Faction, PhaseKind, Role
from padrino.core.rulesets import mini7_v1, sk12_v1
from padrino.db.models import GameEvent, PlacementRating, PlacementRatingEvent, Rating, RatingEvent
from padrino.db.repositories import (
    agent_builds as agent_builds_repo,
)
from padrino.db.repositories import (
    games as games_repo,
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
from padrino.llm.mock import DeterministicMockAdapter
from padrino.ratings.openskill_service import INITIAL_MU, INITIAL_SIGMA
from padrino.runner.game_runner import (
    GameConfig,
    GamePersistence,
    _persist_terminated_event,
    run_game,
)
from tests.conftest import make_town_win_script

_GAME_SEED = "seed-rating-001"


def _split_factions() -> tuple[list[str], list[str], str, str]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return mafia, town, doctor, detective


def _response(action_type: ActionType, target: str | None = None) -> AgentResponse:
    return AgentResponse(
        public_message=None,
        private_message=None,
        action=Action(type=action_type, target=target),
        memory_update="",
        rationale_summary=None,
    )


def _phase_default(phase_id: str) -> AgentResponse:
    if phase_id.endswith("_VOTE"):
        return _response(ActionType.ABSTAIN)
    return _response(ActionType.NOOP)


def _phase_ids_for_sk12() -> tuple[str, ...]:
    phase_ids: list[str] = ["NIGHT_0_MAFIA_INTRO"]
    for day in range(1, sk12_v1.MAX_DAYS + 1):
        for round_index in range(1, sk12_v1.DISCUSSION_ROUNDS_PER_DAY + 1):
            phase_ids.append(f"DAY_{day}_DISCUSSION_ROUND_{round_index}")
        phase_ids.append(f"DAY_{day}_VOTE")
        phase_ids.append(f"NIGHT_{day}_MAFIA_DISCUSSION")
        phase_ids.append(f"NIGHT_{day}_ACTIONS")
    return tuple(phase_ids)


def _sk12_town_win_script(
    *,
    seat_ids: list[str],
    mafia_ids: list[str],
    serial_killer_id: str,
) -> dict[tuple[str, str], AgentResponse]:
    script: dict[tuple[str, str], AgentResponse] = {
        (phase_id, seat_id): _phase_default(phase_id)
        for phase_id in _phase_ids_for_sk12()
        for seat_id in seat_ids
    }
    targets_by_day = {
        1: serial_killer_id,
        2: mafia_ids[0],
        3: mafia_ids[1],
        4: mafia_ids[2],
    }
    for day, target in targets_by_day.items():
        fallback_target = next(seat_id for seat_id in seat_ids if seat_id != target)
        phase_id = f"DAY_{day}_VOTE"
        for seat_id in seat_ids:
            if seat_id == target:
                script[(phase_id, seat_id)] = _response(ActionType.VOTE, fallback_target)
            else:
                script[(phase_id, seat_id)] = _response(ActionType.VOTE, target)
    return script


async def _seed_ranked_setup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ranked: bool,
    hash_prefix: str,
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    """Insert a league, 7 agent builds, and one game row.

    Returns ``(league_id, game_id, agent_builds_by_seat)``.
    """
    async with session_factory() as session, session.begin():
        provider = await providers_repo.create(
            session, name="cerebras", auth_secret_ref="CEREBRAS_API_KEY"
        )
        mc = await model_configs_repo.create(
            session,
            provider_id=provider.id,
            model_name="glm-4.7",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        builds: list[uuid.UUID] = []
        for i in range(mini7_v1.PLAYER_COUNT):
            pv = await prompt_versions_repo.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                version=f"v{i + 1}",
                system_prompt="sys",
                developer_prompt="dev",
                response_schema={"type": "object"},
                prompt_hash=f"{hash_prefix}-{i}",
            )
            ab = await agent_builds_repo.create(
                session,
                display_name=f"build-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="2026.05",
                inference_params={"temperature": 0.7},
                active=True,
            )
            builds.append(ab.id)
        league = await leagues_repo.create(
            session, name="ranked", ruleset_id=mini7_v1.RULESET_ID, ranked=ranked
        )
        game = await games_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status="RUNNING",
        )
        league_id = league.id
        game_id = game.id
    agent_builds_by_seat = {f"P{i + 1:02d}": builds[i] for i in range(mini7_v1.PLAYER_COUNT)}
    return league_id, game_id, agent_builds_by_seat


async def _seed_sk12_placement_setup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_seed: str,
    hash_prefix: str,
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    async with session_factory() as session, session.begin():
        provider = await providers_repo.create(
            session, name="cerebras", auth_secret_ref="CEREBRAS_API_KEY"
        )
        mc = await model_configs_repo.create(
            session,
            provider_id=provider.id,
            model_name="glm-4.7",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        builds: list[uuid.UUID] = []
        for i in range(sk12_v1.PLAYER_COUNT):
            pv = await prompt_versions_repo.create(
                session,
                ruleset_id=sk12_v1.RULESET_ID,
                version=f"sk12-{i + 1}",
                system_prompt="sys",
                developer_prompt="dev",
                response_schema={"type": "object"},
                prompt_hash=f"{hash_prefix}-{i}",
            )
            ab = await agent_builds_repo.create(
                session,
                display_name=f"sk12-build-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="2026.05",
                inference_params={"temperature": 0.7},
                active=True,
            )
            builds.append(ab.id)
        league = await leagues_repo.create(
            session, name="sk12-placement", ruleset_id=sk12_v1.RULESET_ID, ranked=True
        )
        game = await games_repo.create(
            session,
            ruleset_id=sk12_v1.RULESET_ID,
            game_seed=game_seed,
            status="RUNNING",
        )
    agent_builds_by_seat = {f"P{i + 1:02d}": builds[i] for i in range(sk12_v1.PLAYER_COUNT)}
    return league.id, game.id, agent_builds_by_seat


async def test_town_win_updates_town_up_mafia_down(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    league_id, game_id, abs_by_seat = await _seed_ranked_setup(
        session_factory, ranked=True, hash_prefix="us039-town"
    )

    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_id,
        agent_builds=abs_by_seat,
        league_id=league_id,
    )
    outcome = await run_game(
        GameConfig(game_id="G-RATING", game_seed=_GAME_SEED, timeout_s=1.0),
        DeterministicMockAdapter(script),
        ranked=True,
        persistence=persistence,
    )
    assert outcome.final_state.terminal_result == "TOWN"

    async with session_factory() as session:
        rows = (
            (await session.execute(select(Rating).where(Rating.league_id == league_id)))
            .scalars()
            .all()
        )
    by_key = {(r.agent_build_id, r.scope_type, r.scope_value): r for r in rows}

    # Every seat has BOTH a GLOBAL and a FACTION rating row.
    for sid, ab_id in abs_by_seat.items():
        is_mafia = sid in mafia
        global_row = by_key[(ab_id, "GLOBAL", "global")]
        scope_value = "MAFIA" if is_mafia else "TOWN"
        faction_row = by_key[(ab_id, "FACTION", scope_value)]

        if is_mafia:
            assert global_row.mu < INITIAL_MU
            assert faction_row.mu < INITIAL_MU
        else:
            assert global_row.mu > INITIAL_MU
            assert faction_row.mu > INITIAL_MU

        assert global_row.sigma < INITIAL_SIGMA
        assert faction_row.sigma < INITIAL_SIGMA
        assert global_row.games == 1
        assert faction_row.games == 1

    # 7 seats * 2 scopes = 14 rating audit events.
    async with session_factory() as session:
        events = (
            (await session.execute(select(RatingEvent).where(RatingEvent.game_id == game_id)))
            .scalars()
            .all()
        )
    assert len(events) == 14
    for evt in events:
        assert evt.before_mu == pytest.approx(INITIAL_MU)
        assert evt.before_sigma == pytest.approx(INITIAL_SIGMA)
        assert evt.league_id == league_id
        assert evt.scope_type in {"GLOBAL", "FACTION"}


async def test_sk12_terminal_game_writes_placement_ratings_not_scientific_ratings(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_seed = "seed-sk12-placement-runner-001"
    seats = assign_roles(game_seed, sk12_v1)
    seat_ids = [seat.public_player_id for seat in seats]
    mafia_ids = [seat.public_player_id for seat in seats if seat.faction is Faction.MAFIA]
    serial_killer_id = next(
        seat.public_player_id for seat in seats if seat.faction is Faction.SERIAL_KILLER
    )
    script = _sk12_town_win_script(
        seat_ids=seat_ids,
        mafia_ids=mafia_ids,
        serial_killer_id=serial_killer_id,
    )
    league_id, game_id, builds_by_seat = await _seed_sk12_placement_setup(
        session_factory,
        game_seed=game_seed,
        hash_prefix="sk12-runner-placement",
    )

    outcome = await run_game(
        GameConfig(
            game_id="G-SK12-PLACEMENT",
            game_seed=game_seed,
            ruleset_id=sk12_v1.RULESET_ID,
            timeout_s=1.0,
        ),
        DeterministicMockAdapter(script),
        ranked=True,
        persistence=GamePersistence(
            session_factory=session_factory,
            game_id=game_id,
            agent_builds=builds_by_seat,
            league_id=league_id,
        ),
    )

    assert outcome.final_state.terminal_result == Faction.TOWN.value

    async with session_factory() as session:
        scientific_rows = (await session.execute(select(Rating))).scalars().all()
        scientific_events = (await session.execute(select(RatingEvent))).scalars().all()
        placement_rows = (await session.execute(select(PlacementRating))).scalars().all()
        placement_events = (await session.execute(select(PlacementRatingEvent))).scalars().all()

    assert scientific_rows == []
    assert scientific_events == []
    assert len(placement_rows) == sk12_v1.PLAYER_COUNT
    assert len(placement_events) == sk12_v1.PLAYER_COUNT
    assert {event.team_outcome for event in placement_events} == {Faction.TOWN.value}
    assert {event.agent_build_id for event in placement_events} == set(builds_by_seat.values())


async def test_placement_duplicate_build_failure_does_not_rollback_terminal_finalization(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_seed = "seed-sk12-placement-runner-shared-build"
    seats = assign_roles(game_seed, sk12_v1)
    seat_ids = [seat.public_player_id for seat in seats]
    mafia_ids = [seat.public_player_id for seat in seats if seat.faction is Faction.MAFIA]
    serial_killer_id = next(
        seat.public_player_id for seat in seats if seat.faction is Faction.SERIAL_KILLER
    )
    town_seat = next(seat.public_player_id for seat in seats if seat.faction is Faction.TOWN)
    mafia_seat = mafia_ids[0]
    script = _sk12_town_win_script(
        seat_ids=seat_ids,
        mafia_ids=mafia_ids,
        serial_killer_id=serial_killer_id,
    )
    league_id, game_id, builds_by_seat = await _seed_sk12_placement_setup(
        session_factory,
        game_seed=game_seed,
        hash_prefix="sk12-runner-placement-shared-build",
    )
    builds_by_seat[mafia_seat] = builds_by_seat[town_seat]

    outcome = await run_game(
        GameConfig(
            game_id="G-SK12-PLACEMENT-SHARED-BUILD",
            game_seed=game_seed,
            ruleset_id=sk12_v1.RULESET_ID,
            timeout_s=1.0,
        ),
        DeterministicMockAdapter(script),
        ranked=True,
        persistence=GamePersistence(
            session_factory=session_factory,
            game_id=game_id,
            agent_builds=builds_by_seat,
            league_id=league_id,
        ),
    )

    assert outcome.final_state.terminal_result == Faction.TOWN.value

    async with session_factory() as session:
        game = await games_repo.get(session, game_id)
        terminal_events = (
            (
                await session.execute(
                    select(GameEvent).where(
                        GameEvent.game_id == game_id,
                        GameEvent.event_type == "GameTerminated",
                    )
                )
            )
            .scalars()
            .all()
        )
        scientific_rows = (await session.execute(select(Rating))).scalars().all()
        scientific_events = (await session.execute(select(RatingEvent))).scalars().all()
        placement_rows = (await session.execute(select(PlacementRating))).scalars().all()
        placement_events = (await session.execute(select(PlacementRatingEvent))).scalars().all()

    assert game is not None
    assert game.status == "COMPLETED"
    assert game.terminal_result is not None
    assert game.terminal_result["winner"] == Faction.TOWN.value
    assert len(terminal_events) == 1
    assert terminal_events[0].event_hash == game.event_hash_head
    assert scientific_rows == []
    assert scientific_events == []
    assert placement_rows == []
    assert placement_events == []


async def test_placement_non_faction_winner_skips_scoring_after_finalizing_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    game_seed = "seed-sk12-placement-runner-non-faction-winner"
    league_id, game_id, builds_by_seat = await _seed_sk12_placement_setup(
        session_factory,
        game_seed=game_seed,
        hash_prefix="sk12-runner-placement-non-faction-winner",
    )
    state = GameState(
        ruleset_id=sk12_v1.RULESET_ID,
        game_id="G-SK12-PLACEMENT-NON-FACTION-WINNER",
        game_seed=game_seed,
        current_phase=Phase(kind=PhaseKind.TERMINAL, day=1, round=0),
        seats=tuple(assign_roles(game_seed, sk12_v1)),
        day=1,
        terminal_result="JESTER",
        terminal_reason="ALT_TRIGGER",
    )
    event_log = EventLog()
    stored = event_log.append(
        {
            "sequence": 0,
            "event_type": "GameTerminated",
            "phase": "DAY_1_VOTE",
            "visibility": "PUBLIC",
            "actor_player_id": None,
            "payload": {"winner": "JESTER", "reason": "ALT_TRIGGER"},
        }
    )

    await _persist_terminated_event(
        GamePersistence(
            session_factory=session_factory,
            game_id=game_id,
            agent_builds=builds_by_seat,
            league_id=league_id,
        ),
        stored,
        state,
        ranked=True,
        day_terminated=1,
    )

    async with session_factory() as session:
        game = await games_repo.get(session, game_id)
        terminal_events = (
            (
                await session.execute(
                    select(GameEvent).where(
                        GameEvent.game_id == game_id,
                        GameEvent.event_type == "GameTerminated",
                    )
                )
            )
            .scalars()
            .all()
        )
        placement_rows = (await session.execute(select(PlacementRating))).scalars().all()
        placement_events = (await session.execute(select(PlacementRatingEvent))).scalars().all()

    assert game is not None
    assert game.status == "COMPLETED"
    assert game.terminal_result == {
        "winner": "JESTER",
        "reason": "ALT_TRIGGER",
        "day_terminated": 1,
    }
    assert len(terminal_events) == 1
    assert terminal_events[0].event_hash == game.event_hash_head
    assert placement_rows == []
    assert placement_events == []


async def test_unranked_game_skips_rating_updates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    league_id, game_id, abs_by_seat = await _seed_ranked_setup(
        session_factory, ranked=False, hash_prefix="us039-unranked"
    )

    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_id,
        agent_builds=abs_by_seat,
        league_id=league_id,
    )
    await run_game(
        GameConfig(game_id="G-RATING-U", game_seed=_GAME_SEED, timeout_s=1.0),
        DeterministicMockAdapter(script),
        ranked=False,
        persistence=persistence,
    )

    async with session_factory() as session:
        ratings = (await session.execute(select(Rating))).scalars().all()
        events = (await session.execute(select(RatingEvent))).scalars().all()
    assert ratings == []
    assert events == []


async def test_persistence_without_league_id_skips_rating_updates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    _, game_id, abs_by_seat = await _seed_ranked_setup(
        session_factory, ranked=True, hash_prefix="us039-noleague"
    )

    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_id,
        agent_builds=abs_by_seat,
        league_id=None,
    )
    await run_game(
        GameConfig(game_id="G-RATING-NL", game_seed=_GAME_SEED, timeout_s=1.0),
        DeterministicMockAdapter(script),
        ranked=True,
        persistence=persistence,
    )

    async with session_factory() as session:
        ratings = (await session.execute(select(Rating))).scalars().all()
    assert ratings == []


async def test_rating_idempotency(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    league_id, game_id, abs_by_seat = await _seed_ranked_setup(
        session_factory, ranked=True, hash_prefix="us039-idempotency"
    )

    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_id,
        agent_builds=abs_by_seat,
        league_id=league_id,
    )
    outcome = await run_game(
        GameConfig(game_id="G-RATING-IDEM", game_seed=_GAME_SEED, timeout_s=1.0),
        DeterministicMockAdapter(script),
        ranked=True,
        persistence=persistence,
    )
    assert outcome.final_state.terminal_result == "TOWN"

    # Verify initial 14 rating events exist
    async with session_factory() as session:
        events = (
            (await session.execute(select(RatingEvent).where(RatingEvent.game_id == game_id)))
            .scalars()
            .all()
        )
    assert len(events) == 14

    # Trigger termination again on the completed game.
    # It must bypass rating updates due to game.status == 'COMPLETED' and not raise an IntegrityError.
    stored = StoredEvent(
        sequence=100,
        event_hash="fake-hash",
        prev_event_hash="prev-fake-hash",
        body={
            "event_type": "GameTerminated",
            "phase": "TERMINAL",
            "visibility": "PUBLIC",
            "payload": {"winner": "TOWN", "reason": "test"},
        },
    )
    await _persist_terminated_event(
        persistence,
        stored,
        outcome.final_state,
        ranked=True,
        day_terminated=5,
    )

    # Assert that NO new rating events were written (still 14)
    async with session_factory() as session:
        events_after = (
            (await session.execute(select(RatingEvent).where(RatingEvent.game_id == game_id)))
            .scalars()
            .all()
        )
    assert len(events_after) == 14
