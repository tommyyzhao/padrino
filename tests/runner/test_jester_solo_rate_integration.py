"""US-185: Jester alt-win games write only SOLO_RATE records."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.replay import replay_event_log
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import ActionType, Role
from padrino.core.rulesets import jester8_v1
from padrino.db.models import (
    PlacementRating,
    PlacementRatingEvent,
    Rating,
    RatingEvent,
    SoloRateRating,
    SoloRateRatingEvent,
)
from padrino.db.repositories import (
    agent_builds,
    games,
    leagues,
    model_configs,
    prompt_versions,
    providers,
)
from padrino.llm.mock import DeterministicMockAdapter
from padrino.runner.game_runner import GameConfig, GamePersistence, run_game

_GAME_SEED = "seed-jester-solo-rate-001"


def _response(action_type: ActionType, target: str | None = None) -> AgentResponse:
    return AgentResponse(
        public_message=None,
        private_message=None,
        action=Action(type=action_type, target=target),
        memory_update="",
        rationale_summary=None,
    )


def _phase_ids() -> tuple[str, ...]:
    out: list[str] = ["NIGHT_0_MAFIA_INTRO"]
    for day in range(1, jester8_v1.MAX_DAYS + 1):
        for round_index in range(1, jester8_v1.DISCUSSION_ROUNDS_PER_DAY + 1):
            out.append(f"DAY_{day}_DISCUSSION_ROUND_{round_index}")
        out.append(f"DAY_{day}_VOTE")
        out.append(f"NIGHT_{day}_MAFIA_DISCUSSION")
        out.append(f"NIGHT_{day}_ACTIONS")
    return tuple(out)


def _lynch_jester_script(
    seat_ids: list[str],
    *,
    jester_id: str,
) -> dict[tuple[str, str], AgentResponse]:
    script: dict[tuple[str, str], AgentResponse] = {}
    fallback_target = next(seat_id for seat_id in seat_ids if seat_id != jester_id)
    for phase_id in _phase_ids():
        for seat_id in seat_ids:
            if phase_id == "DAY_1_VOTE":
                target = fallback_target if seat_id == jester_id else jester_id
                script[(phase_id, seat_id)] = _response(ActionType.VOTE, target)
            elif phase_id.endswith("_VOTE"):
                script[(phase_id, seat_id)] = _response(ActionType.ABSTAIN)
            else:
                script[(phase_id, seat_id)] = _response(ActionType.NOOP)
    return script


async def _seed_jester_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    async with session_factory() as session, session.begin():
        provider = await providers.create(
            session, name="cerebras", auth_secret_ref="CEREBRAS_API_KEY"
        )
        mc = await model_configs.create(
            session,
            provider_id=provider.id,
            model_name="glm-4.7",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        builds: dict[str, uuid.UUID] = {}
        for i in range(jester8_v1.PLAYER_COUNT):
            seat_id = f"P{i + 1:02d}"
            pv = await prompt_versions.create(
                session,
                ruleset_id=jester8_v1.RULESET_ID,
                version=f"jester-{i + 1}",
                system_prompt="sys",
                developer_prompt="dev",
                response_schema={"type": "object"},
                prompt_hash=f"jester-solo-rate-{i}",
            )
            ab = await agent_builds.create(
                session,
                display_name=f"jester-build-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="2026.05",
                inference_params={"temperature": 0.7},
                active=True,
            )
            builds[seat_id] = ab.id
        league = await leagues.create(
            session,
            name="jester-solo-rate",
            ruleset_id=jester8_v1.RULESET_ID,
            ranked=True,
        )
        game = await games.create(
            session,
            ruleset_id=jester8_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status="RUNNING",
        )
    return league.id, game.id, builds


async def _table_count(
    session: AsyncSession,
    model: type[object],
) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_jester_alt_win_persisted_game_writes_only_solo_rate_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seats = assign_roles(_GAME_SEED, jester8_v1)
    jester_id = next(seat.public_player_id for seat in seats if seat.role is Role.JESTER)
    script = _lynch_jester_script(
        [seat.public_player_id for seat in seats],
        jester_id=jester_id,
    )
    league_id, game_id, builds = await _seed_jester_game(session_factory)

    outcome = await run_game(
        GameConfig(
            game_id="G-JESTER-SOLO-RATE",
            game_seed=_GAME_SEED,
            ruleset_id=jester8_v1.RULESET_ID,
            timeout_s=1.0,
        ),
        DeterministicMockAdapter(script),
        ranked=True,
        persistence=GamePersistence(
            session_factory=session_factory,
            game_id=game_id,
            agent_builds=builds,
            league_id=league_id,
        ),
    )

    assert outcome.final_state.terminal_result == jester8_v1.JESTER_WINNER
    assert outcome.final_state.terminal_reason == jester8_v1.REASON_JESTER_DAY_VOTED_OUT
    replay_event_log(outcome.event_log.events)

    async with session_factory() as session:
        canonical_rows = await _table_count(session, Rating)
        canonical_events = await _table_count(session, RatingEvent)
        placement_rows = await _table_count(session, PlacementRating)
        placement_events = await _table_count(session, PlacementRatingEvent)
        solo_rows = (await session.execute(select(SoloRateRating))).scalars().all()
        solo_events = (await session.execute(select(SoloRateRatingEvent))).scalars().all()
        jester_build_id = builds[jester_id]

    assert canonical_rows == 0
    assert canonical_events == 0
    assert placement_rows == 0
    assert placement_events == 0
    assert len(solo_rows) == 1
    assert len(solo_events) == 1
    assert solo_rows[0].agent_build_id == jester_build_id
    assert solo_rows[0].scope_value == Role.JESTER.value
    assert (solo_rows[0].successes, solo_rows[0].attempts) == (1, 1)
    assert solo_events[0].public_player_id == jester_id
    assert solo_events[0].outcome_label == jester8_v1.JESTER_OUTCOME_LABEL
    assert solo_events[0].scope_value == Role.JESTER.value
    assert solo_events[0].succeeded is True
