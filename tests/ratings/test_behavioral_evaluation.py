"""Tests for the post-game LLM judge behavioral evaluation pipeline (Wave 6)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.hashing import GENESIS_HASH, compute_event_hash
from padrino.core.rulesets import mini7_v1
from padrino.db.models import BehavioralEvaluation
from padrino.db.repositories import (
    agent_builds as agent_builds_repo,
)
from padrino.db.repositories import (
    events as events_repo,
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
from padrino.ratings.evaluator import (
    evaluate_completed_game_behavioral,
    run_pending_behavioral_evaluations,
)
from padrino.settings import Settings


class MockJudgeAdapter:
    """Mock LLM judge that returns a predefined JSON payload."""

    def __init__(self, response_dict: dict[str, Any]) -> None:
        self.response_dict = response_dict
        self.calls: list[str] = []

    async def complete_judge(self, prompt: str) -> str:
        self.calls.append(prompt)
        return json.dumps(self.response_dict)


async def _seed_world(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
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
    pv = await prompt_versions_repo.create(
        session,
        ruleset_id=mini7_v1.RULESET_ID,
        version="v1",
        system_prompt="sys",
        developer_prompt="dev",
        response_schema={"type": "object"},
        prompt_hash=f"eval-{uuid.uuid4().hex}",
    )
    league = await leagues_repo.create(
        session, name="Eval League", ruleset_id=mini7_v1.RULESET_ID, ranked=True
    )
    roster: list[uuid.UUID] = []
    for i in range(mini7_v1.PLAYER_COUNT):
        ab = await agent_builds_repo.create(
            session,
            display_name=f"seat-{i}",
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version="2026.05",
            inference_params={},
            active=True,
        )
        roster.append(ab.id)
    return league.id, pv.id, roster


async def _seed_seats_and_complete_game(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    roster: list[uuid.UUID],
) -> None:
    # 7 players mini7_v1 conventions
    roles = [
        ("MAFIA_GOON", "MAFIA"),
        ("MAFIA_GOON", "MAFIA"),
        ("DETECTIVE", "TOWN"),
        ("DOCTOR", "TOWN"),
        ("VILLAGER", "TOWN"),
        ("VILLAGER", "TOWN"),
        ("VILLAGER", "TOWN"),
    ]
    for i, ab_id in enumerate(roster):
        role, faction = roles[i]
        await games_repo.add_seat(
            session,
            game_id=game_id,
            public_player_id=f"P{i + 1:02d}",
            seat_index=i,
            agent_build_id=ab_id,
            role=role,
            faction=faction,
            alive=True,
        )

    # Append events
    bodies: list[dict[str, Any]] = [
        {
            "event_type": "PublicMessageSubmitted",
            "phase": "DAY_1_DISCUSSION_ROUND_1",
            "visibility": "PUBLIC",
            "actor_player_id": "P01",
            "payload": {"text": "Hello, I am innocent villager.", "round_index": 0},
        },
        {
            "event_type": "PrivateMessageSubmitted",
            "phase": "NIGHT_1_MAFIA_DISCUSSION",
            "visibility": "PRIVATE",
            "actor_player_id": "P01",
            "payload": {"text": "Let's target P03 tonight.", "channel_id": "MAFIA"},
        },
        {
            "event_type": "GameTerminated",
            "phase": "TERMINAL",
            "visibility": "PUBLIC",
            "actor_player_id": None,
            "payload": {"winner": "MAFIA", "reason": "mafia_outnumber_town", "day_terminated": 2},
        },
    ]

    prev = GENESIS_HASH
    for i, body in enumerate(bodies):
        sealed = dict(body)
        sealed["sequence"] = i
        ev_hash = compute_event_hash(prev, sealed)
        await events_repo.append_event(
            session,
            game_id=game_id,
            sequence=sealed["sequence"],
            event_type=sealed["event_type"],
            phase=sealed["phase"],
            visibility=sealed["visibility"],
            actor_player_id=sealed.get("actor_player_id"),
            payload=dict(sealed.get("payload", {})),
            prev_event_hash=prev,
            event_hash=ev_hash,
        )
        prev = ev_hash

    await games_repo.update_status(
        session,
        game_id,
        status="COMPLETED",
        terminal_result={"winner": "MAFIA", "reason": "mafia_outnumber_town", "day_terminated": 2},
    )


async def test_evaluate_completed_game_behavioral_success(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        _league_id, _pv_id, roster = await _seed_world(session)
        game = await games_repo.create(
            session, ruleset_id=mini7_v1.RULESET_ID, game_seed="eval-seed"
        )
        await _seed_seats_and_complete_game(session, game_id=game.id, roster=roster)

    # Pre-built judge output
    judge_data = {
        "evaluations": [
            {
                "public_player_id": "P01",
                "persuasion_score": 4,
                "deception_score": 5,
                "logical_consistency_score": 3,
                "social_heuristics_score": 4,
                "written_feedback": "Exceeded expectations in deception by leading mafia team strategy.",
            },
            {
                "public_player_id": "P02",
                "persuasion_score": 3,
                "deception_score": 4,
                "logical_consistency_score": 4,
                "social_heuristics_score": 3,
                "written_feedback": "Solid deception, backed up P01 claims well.",
            },
            {
                "public_player_id": "P03",
                "persuasion_score": 2,
                "deception_score": 1,
                "logical_consistency_score": 5,
                "social_heuristics_score": 2,
                "written_feedback": "Great logic but was isolated socially.",
            },
        ]
    }

    mock_adapter = MockJudgeAdapter(judge_data)

    async with session_factory() as session, session.begin():
        evaluations = await evaluate_completed_game_behavioral(session, game.id, mock_adapter)

    assert len(evaluations) == mini7_v1.PLAYER_COUNT
    assert len(mock_adapter.calls) == 1
    assert "=== CHRONOLOGICAL GAME LOG ===" in mock_adapter.calls[0]
    assert "P01: Hello, I am innocent villager." in mock_adapter.calls[0]

    # Check that P01 was correctly persisted
    p01_eval = next(e for e in evaluations if e.public_player_id == "P01")
    assert p01_eval.persuasion_score == 4
    assert p01_eval.deception_score == 5
    assert p01_eval.logical_consistency_score == 3
    assert p01_eval.social_heuristics_score == 4
    assert (
        p01_eval.written_feedback
        == "Exceeded expectations in deception by leading mafia team strategy."
    )
    assert p01_eval.agent_build_id == roster[0]

    # Check safe defaults for un-scored seats (e.g. P04)
    p04_eval = next(e for e in evaluations if e.public_player_id == "P04")
    assert p04_eval.persuasion_score == 3
    assert p04_eval.deception_score == 3
    assert p04_eval.written_feedback == "No specific feedback provided by judge."

    # Ensure they are saved in the DB
    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(BehavioralEvaluation).where(BehavioralEvaluation.game_id == game.id)
                )
            ).scalars()
        )
        assert len(rows) == mini7_v1.PLAYER_COUNT


async def test_evaluate_game_behavioral_errors_on_uncompleted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        _league_id, _pv_id, roster = await _seed_world(session)
        game = await games_repo.create(
            session, ruleset_id=mini7_v1.RULESET_ID, game_seed="eval-seed"
        )
        # seed seats but keep status as CREATED
        roles = [("VILLAGER", "TOWN")] * mini7_v1.PLAYER_COUNT
        for i, ab_id in enumerate(roster):
            role, faction = roles[i]
            await games_repo.add_seat(
                session,
                game_id=game.id,
                public_player_id=f"P{i + 1:02d}",
                seat_index=i,
                agent_build_id=ab_id,
                role=role,
                faction=faction,
            )

    mock_adapter = MockJudgeAdapter({"evaluations": []})

    async with session_factory() as session:
        with pytest.raises(ValueError, match="is not in COMPLETED status"):
            await evaluate_completed_game_behavioral(session, game.id, mock_adapter)


async def test_evaluate_game_behavioral_errors_on_unknown_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mock_adapter = MockJudgeAdapter({"evaluations": []})
    async with session_factory() as session:
        with pytest.raises(ValueError, match="not found"):
            await evaluate_completed_game_behavioral(session, uuid.uuid4(), mock_adapter)


async def test_evaluate_completed_game_behavioral_uniqueness(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        _league_id, _pv_id, roster = await _seed_world(session)
        game = await games_repo.create(
            session, ruleset_id=mini7_v1.RULESET_ID, game_seed="eval-seed"
        )
        await _seed_seats_and_complete_game(session, game_id=game.id, roster=roster)

    judge_data = {
        "evaluations": [
            {
                "public_player_id": "P01",
                "persuasion_score": 4,
                "deception_score": 5,
                "logical_consistency_score": 3,
                "social_heuristics_score": 4,
                "written_feedback": "Feedback",
            }
        ]
    }
    mock_adapter = MockJudgeAdapter(judge_data)

    async with session_factory() as session, session.begin():
        first_runs = await evaluate_completed_game_behavioral(session, game.id, mock_adapter)
        assert len(first_runs) == mini7_v1.PLAYER_COUNT

    # Re-running evaluation on the same game shouldn't add duplicate rows due to the uniqueness safety check
    async with session_factory() as session, session.begin():
        second_runs = await evaluate_completed_game_behavioral(session, game.id, mock_adapter)
        assert len(second_runs) == 0


async def test_run_pending_behavioral_evaluations_job(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session, session.begin():
        _league_id, _pv_id, roster = await _seed_world(session)
        game = await games_repo.create(
            session, ruleset_id=mini7_v1.RULESET_ID, game_seed="eval-seed"
        )
        await _seed_seats_and_complete_game(session, game_id=game.id, roster=roster)

    judge_data = {
        "evaluations": [
            {
                "public_player_id": "P01",
                "persuasion_score": 4,
                "deception_score": 5,
                "logical_consistency_score": 3,
                "social_heuristics_score": 4,
                "written_feedback": "Feedback",
            }
        ]
    }

    # Mock evaluate_completed_game_behavioral call to use mock adapter
    from padrino.ratings import evaluator

    original_fn = evaluator.evaluate_completed_game_behavioral

    async def mocked_eval(
        session: AsyncSession, game_id: uuid.UUID, judge_adapter: Any = None
    ) -> list[BehavioralEvaluation]:
        return await original_fn(session, game_id, MockJudgeAdapter(judge_data))

    monkeypatch.setattr(evaluator, "evaluate_completed_game_behavioral", mocked_eval)

    settings = Settings()
    await run_pending_behavioral_evaluations(session_factory, settings=settings)

    # Assert they are fully persisted
    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(BehavioralEvaluation).where(BehavioralEvaluation.game_id == game.id)
                )
            ).scalars()
        )
        assert len(rows) == mini7_v1.PLAYER_COUNT
