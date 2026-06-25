"""US-049: ``GameTerminated`` event + Game status + ratings commit atomically.

The runner writes (1) the terminal event row, (2) ``Game.status='COMPLETED'``
plus the JSON ``Game.terminal_result``, and (3) rating updates inside a
single ``session.begin()``. A failure mid-transaction must roll back all
three — nothing is partially persisted.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.event_log import EventLog
from padrino.core.engine.reducer import initial_state
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, Role
from padrino.core.rulesets import mini7_v1
from padrino.db.models import Game, GameEvent, Rating, RatingEvent
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
from padrino.runner import game_runner
from padrino.runner.game_runner import GameConfig, GamePersistence, run_game
from tests.conftest import make_town_win_script

_GAME_SEED = "seed-us049-terminal"


def _split_factions() -> tuple[list[str], list[str], str, str]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return mafia, town, doctor, detective


async def _seed_ranked_setup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    hash_prefix: str,
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    async with session_factory() as session, session.begin():
        provider = await providers_repo.create(session, name="p", auth_secret_ref="env:X")
        mc = await model_configs_repo.create(
            session,
            provider_id=provider.id,
            model_name="m",
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
                version=f"v{i}",
                system_prompt="s",
                developer_prompt="d",
                response_schema={"type": "object"},
                prompt_hash=f"{hash_prefix}-{i}",
            )
            ab = await agent_builds_repo.create(
                session,
                display_name=f"b-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="v",
                inference_params={},
                active=True,
            )
            builds.append(ab.id)
        league = await leagues_repo.create(
            session, name="lg", ruleset_id=mini7_v1.RULESET_ID, ranked=True
        )
        game = await games_repo.create(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed=_GAME_SEED,
            status="RUNNING",
        )
        league_id, game_id = league.id, game.id
    builds_by_seat = {f"P{i + 1:02d}": builds[i] for i in range(mini7_v1.PLAYER_COUNT)}
    return league_id, game_id, builds_by_seat


async def test_terminal_event_status_and_terminal_result_committed_together(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    league_id, game_id, builds_by_seat = await _seed_ranked_setup(
        session_factory, hash_prefix="us049-ok"
    )
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )

    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_id,
        agent_builds=builds_by_seat,
        league_id=league_id,
    )
    await run_game(
        GameConfig(game_id="G-TERM-OK", game_seed=_GAME_SEED, timeout_s=1.0),
        DeterministicMockAdapter(script),
        ranked=True,
        persistence=persistence,
    )

    async with session_factory() as session:
        game = await session.get(Game, game_id)
        assert game is not None
        assert game.status == "COMPLETED"
        assert game.terminal_result is not None
        assert game.terminal_result["winner"] == "TOWN"
        assert isinstance(game.terminal_result["reason"], str)
        assert isinstance(game.terminal_result["day_terminated"], int)
        assert game.terminal_result["day_terminated"] >= 1

        terminated = (
            await session.execute(
                select(GameEvent).where(
                    GameEvent.game_id == game_id,
                    GameEvent.event_type == "GameTerminated",
                )
            )
        ).scalar_one()
        # The game-row's terminal_result mirrors the event payload's winner/reason.
        assert game.terminal_result["winner"] == terminated.payload["winner"]
        assert game.terminal_result["reason"] == terminated.payload["reason"]


async def test_terminal_rollback_when_ratings_fail_leaves_no_partial_writes(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure inside the terminal txn must roll back event + game-row + ratings."""
    league_id, game_id, builds_by_seat = await _seed_ranked_setup(
        session_factory, hash_prefix="us049-rb"
    )
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )

    class _Boom(RuntimeError):
        pass

    async def boom(*args: object, **kwargs: object) -> None:
        raise _Boom("rating update exploded")

    monkeypatch.setattr(game_runner, "update_ratings_for_game", boom)

    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_id,
        agent_builds=builds_by_seat,
        league_id=league_id,
    )
    with pytest.raises(_Boom):
        await run_game(
            GameConfig(game_id="G-TERM-RB", game_seed=_GAME_SEED, timeout_s=1.0),
            DeterministicMockAdapter(script),
            ranked=True,
            persistence=persistence,
        )

    async with session_factory() as session:
        # Game row keeps its pre-run status; terminal_result stays None.
        game = await session.get(Game, game_id)
        assert game is not None
        assert game.status == "RUNNING"
        assert game.terminal_result is None

        # No GameTerminated row persisted — the whole txn rolled back.
        terminated = (
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
        assert terminated == []

        # No ratings or rating-events persisted either.
        ratings = (await session.execute(select(Rating))).scalars().all()
        rating_events = (await session.execute(select(RatingEvent))).scalars().all()
        assert ratings == []
        assert rating_events == []


async def test_completed_game_terminal_persist_skip_writes_no_extra_ratings(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    league_id, game_id, builds_by_seat = await _seed_ranked_setup(
        session_factory, hash_prefix="us250-completed-skip"
    )
    terminal_result = {"winner": "TOWN", "reason": "NO_MAFIA_ALIVE", "day_terminated": 2}
    async with session_factory() as session, session.begin():
        await games_repo.update_status(
            session,
            game_id,
            status="COMPLETED",
            terminal_result=terminal_result,
        )

    async def fail_rating_update(*args: object, **kwargs: object) -> None:
        raise AssertionError("completed game must not apply ratings again")

    monkeypatch.setattr(game_runner, "update_ratings_for_game", fail_rating_update)
    event_log = EventLog()
    stored = event_log.append(
        {
            "event_type": "GameTerminated",
            "sequence": 0,
            "phase": "DAY_2_VOTE",
            "visibility": "PUBLIC",
            "actor_player_id": None,
            "payload": {"winner": "TOWN", "reason": "NO_MAFIA_ALIVE"},
        }
    )
    state = initial_state().model_copy(
        update={
            "ruleset_id": mini7_v1.RULESET_ID,
            "game_id": str(game_id),
            "game_seed": _GAME_SEED,
            "seats": tuple(assign_roles(_GAME_SEED, mini7_v1)),
            "terminal_result": "TOWN",
            "terminal_reason": "NO_MAFIA_ALIVE",
        }
    )
    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_id,
        agent_builds=builds_by_seat,
        league_id=league_id,
    )

    await game_runner._persist_terminated_event(
        persistence,
        stored,
        state,
        ranked=True,
        day_terminated=2,
    )

    async with session_factory() as session:
        game = await session.get(Game, game_id)
        game_events = list((await session.execute(select(GameEvent))).scalars().all())
        rating_events = list((await session.execute(select(RatingEvent))).scalars().all())

    assert game is not None
    assert game.status == "COMPLETED"
    assert game.terminal_result == terminal_result
    assert game_events == []
    assert rating_events == []
