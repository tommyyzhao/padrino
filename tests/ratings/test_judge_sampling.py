"""Tests for sampled batch judge enrichment job (US-105)."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.hashing import GENESIS_HASH, compute_event_hash
from padrino.core.rulesets import mini7_v1
from padrino.db.models import (
    BehavioralEvaluation,
    JudgeEnrichmentCard,
    LlmCall,
    Rating,
    RatingEvent,
)
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
    model_configs as model_configs_repo,
)
from padrino.db.repositories import (
    prompt_versions as prompt_versions_repo,
)
from padrino.db.repositories import (
    providers as providers_repo,
)
from padrino.ratings.judge_sampling import run_sampled_judge_enrichment
from padrino.settings import Settings


class MockJudgeAdapter:
    """Mock LLM judge returning fixed scores for all 7 players."""

    def __init__(self, scores: dict[str, int] | None = None) -> None:
        self.calls: list[str] = []
        self._scores = scores or {
            "persuasion": 3,
            "deception": 4,
            "logical_consistency": 2,
            "social_heuristics": 5,
        }

    async def complete_judge(self, prompt: str) -> str:
        self.calls.append(prompt)
        evals = []
        for i in range(1, mini7_v1.PLAYER_COUNT + 1):
            evals.append(
                {
                    "public_player_id": f"P{i:02d}",
                    "persuasion_score": self._scores["persuasion"],
                    "deception_score": self._scores["deception"],
                    "logical_consistency_score": self._scores["logical_consistency"],
                    "social_heuristics_score": self._scores["social_heuristics"],
                    "written_feedback": "Solid performance.",
                }
            )
        return json.dumps({"evaluations": evals})


async def _seed_world(session: AsyncSession) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Create provider → model config → prompt version → 7 agent builds."""
    provider = await providers_repo.create(
        session, name="test-provider", auth_secret_ref="TEST_KEY"
    )
    mc = await model_configs_repo.create(
        session,
        provider_id=provider.id,
        model_name="test-model",
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
        prompt_hash=f"sampling-{uuid.uuid4().hex}",
    )
    roster: list[uuid.UUID] = []
    for i in range(mini7_v1.PLAYER_COUNT):
        ab = await agent_builds_repo.create(
            session,
            display_name=f"agent-{i}",
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version="2026.05",
            inference_params={},
            active=True,
        )
        roster.append(ab.id)
    return pv.id, roster


async def _seed_completed_game(
    session: AsyncSession,
    roster: list[uuid.UUID],
    *,
    seed: str = "test-seed",
) -> uuid.UUID:
    """Create a completed game with 7 seated agents and a minimal event log."""
    game = await games_repo.create(session, ruleset_id=mini7_v1.RULESET_ID, game_seed=seed)
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
            game_id=game.id,
            public_player_id=f"P{i + 1:02d}",
            seat_index=i,
            agent_build_id=ab_id,
            role=role,
            faction=faction,
            alive=True,
        )

    bodies: list[dict[str, Any]] = [
        {
            "event_type": "PublicMessageSubmitted",
            "phase": "DAY_1_DISCUSSION_ROUND_1",
            "visibility": "PUBLIC",
            "actor_player_id": "P01",
            "payload": {"text": "I am innocent.", "round_index": 0},
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
            game_id=game.id,
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
        game.id,
        status="COMPLETED",
        terminal_result={"winner": "MAFIA", "reason": "mafia_outnumber_town", "day_terminated": 2},
    )
    return game.id


async def test_sampling_rate_selects_fraction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With sample_rate=0.5 and 4 eligible games, exactly 2 get evaluated."""
    async with session_factory() as session, session.begin():
        _pv_id, roster = await _seed_world(session)
        for k in range(4):
            await _seed_completed_game(session, roster, seed=f"s-rate-seed-{k}")

    adapter = MockJudgeAdapter()
    settings = Settings(padrino_judge_sample_rate=0.5, padrino_judge_max_games_per_run=100)
    n = await run_sampled_judge_enrichment(
        session_factory, settings=settings, judge_adapter=adapter
    )

    assert n == 2
    assert len(adapter.calls) == 2


async def test_cap_stop_limits_games(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With max_games_per_run=2 and 6 eligible games, only 2 are processed."""
    async with session_factory() as session, session.begin():
        _pv_id, roster = await _seed_world(session)
        for k in range(6):
            await _seed_completed_game(session, roster, seed=f"cap-seed-{k}")

    adapter = MockJudgeAdapter()
    settings = Settings(padrino_judge_sample_rate=1.0, padrino_judge_max_games_per_run=2)
    n = await run_sampled_judge_enrichment(
        session_factory, settings=settings, judge_adapter=adapter
    )

    assert n == 2
    assert len(adapter.calls) == 2


async def test_judge_output_never_writes_rating_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Running judge enrichment must not create any Rating or RatingEvent rows."""
    async with session_factory() as session, session.begin():
        _pv_id, roster = await _seed_world(session)
        await _seed_completed_game(session, roster, seed="no-rating-seed")

    settings = Settings(padrino_judge_sample_rate=1.0, padrino_judge_max_games_per_run=10)
    await run_sampled_judge_enrichment(
        session_factory, settings=settings, judge_adapter=MockJudgeAdapter()
    )

    async with session_factory() as session:
        rating_count = (await session.execute(select(Rating))).scalars().all()
        rating_event_count = (await session.execute(select(RatingEvent))).scalars().all()
        enrichment_count = (await session.execute(select(JudgeEnrichmentCard))).scalars().all()

    assert len(rating_count) == 0, "Judge enrichment must not write Rating rows"
    assert len(rating_event_count) == 0, "Judge enrichment must not write RatingEvent rows"
    assert len(enrichment_count) > 0, "JudgeEnrichmentCard rows should be created"


async def test_global_spend_cap_gates_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When global spend cap is already reached, the enrichment run is skipped (returns 0)."""
    async with session_factory() as session, session.begin():
        _pv_id, roster = await _seed_world(session)
        game_id = await _seed_completed_game(session, roster, seed="spend-cap-seed")
        # Seed a LlmCall that pushes cumulative spend over the (tiny) cap
        session.add(
            LlmCall(
                game_id=game_id,
                public_player_id="P01",
                phase="DAY_1",
                request_json={},
                request_prompt_hash="hash-001",
                status="ok",
                cost_usd=999.0,
            )
        )

    settings = Settings(
        padrino_global_spend_cap_usd=0.01,
        padrino_judge_sample_rate=1.0,
        padrino_judge_max_games_per_run=10,
    )
    n = await run_sampled_judge_enrichment(
        session_factory, settings=settings, judge_adapter=MockJudgeAdapter()
    )

    assert n == 0

    # Confirm no evaluations were created
    async with session_factory() as session:
        evals = (await session.execute(select(BehavioralEvaluation))).scalars().all()
    assert len(evals) == 0


async def test_no_eligible_games_returns_zero(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When there are no unevaluated completed games, the run returns 0."""
    settings = Settings(padrino_judge_sample_rate=1.0, padrino_judge_max_games_per_run=10)
    n = await run_sampled_judge_enrichment(
        session_factory, settings=settings, judge_adapter=MockJudgeAdapter()
    )
    assert n == 0


async def test_enrichment_cards_aggregated_with_correct_averages(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After processing 2 games for the same agent+role, the enrichment card averages are correct."""
    async with session_factory() as session, session.begin():
        _pv_id, roster = await _seed_world(session)
        # Seed 2 games; agent at index 4 plays VILLAGER in both
        for k in range(2):
            await _seed_completed_game(session, roster, seed=f"agg-seed-{k}")

    fixed_scores = {
        "persuasion": 2,
        "deception": 4,
        "logical_consistency": 3,
        "social_heuristics": 5,
    }
    settings = Settings(padrino_judge_sample_rate=1.0, padrino_judge_max_games_per_run=10)
    n = await run_sampled_judge_enrichment(
        session_factory,
        settings=settings,
        judge_adapter=MockJudgeAdapter(scores=fixed_scores),
    )

    assert n == 2

    # Agent 4 (index 4, role VILLAGER/TOWN) should have an enrichment card
    villager_agent_id = roster[4]
    async with session_factory() as session:
        stmt = select(JudgeEnrichmentCard).where(
            JudgeEnrichmentCard.agent_build_id == villager_agent_id,
            JudgeEnrichmentCard.role == "VILLAGER",
            JudgeEnrichmentCard.ruleset_id == mini7_v1.RULESET_ID,
        )
        card = (await session.execute(stmt)).scalar_one_or_none()

    assert card is not None
    assert card.games_count == 2
    assert abs(card.avg_persuasion - 2.0) < 0.01
    assert abs(card.avg_deception - 4.0) < 0.01
    assert abs(card.avg_logical_consistency - 3.0) < 0.01
    assert abs(card.avg_social_heuristics - 5.0) < 0.01


async def test_already_evaluated_games_skipped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Games already in BehavioralEvaluation are not re-evaluated."""
    async with session_factory() as session, session.begin():
        _pv_id, roster = await _seed_world(session)
        await _seed_completed_game(session, roster, seed="already-done-seed")

    adapter = MockJudgeAdapter()
    settings = Settings(padrino_judge_sample_rate=1.0, padrino_judge_max_games_per_run=10)

    # First run evaluates the game
    n1 = await run_sampled_judge_enrichment(
        session_factory, settings=settings, judge_adapter=adapter
    )
    assert n1 == 1
    assert len(adapter.calls) == 1

    # Second run finds no unevaluated games
    n2 = await run_sampled_judge_enrichment(
        session_factory, settings=settings, judge_adapter=adapter
    )
    assert n2 == 0
    assert len(adapter.calls) == 1  # no additional judge calls
