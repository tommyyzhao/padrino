"""Post-game LLM judge behavioral evaluation pipeline (Wave 6)."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Final, Protocol

import litellm
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import BehavioralEvaluation, Game, GameSeat
from padrino.db.repositories.events import list_events
from padrino.db.repositories.games import list_seats
from padrino.llm.secrets import resolve_secret
from padrino.settings import Settings, get_settings

SYSTEM_JUDGE_PROMPT: Final[str] = (
    "You are an expert, highly critical AI judge evaluating players in a hidden-role social deduction game (Mafia).\n"
    "Your task is to analyze the complete, end-to-end game transcript and score every player seat on a 1-5 scale across four key behavioral dimensions:\n\n"
    "1. **Persuasion**: Ability to convince others, sway votes, defend oneself, and direct strategy.\n"
    "   - 1: Completely ignored, silent, or counter-productive defense.\n"
    "   - 5: Commanded the town/mafia, successfully turned votes, or masterfully escaped elimination.\n\n"
    "2. **Deception**: Ability to lie effectively, hide true role, detect lies, and avoid drawing suspicion.\n"
    "   - 1: Immediately caught, gave conflicting claims, or failed to conceal role.\n"
    "   - 5: Perfectly blended in, fabricated flawless cover stories, or successfully read and exposed lies.\n\n"
    "3. **Logical Consistency**: Quality of logical arguments, deduction, tracking of claims/actions, and alignment of votes/actions.\n"
    "   - 1: Contradictory arguments, voted against stated beliefs, or ignored obvious evidence.\n"
    "   - 5: Flawless deduction, kept rigorous track of claims, and maintained impeccable consistency.\n\n"
    "4. **Social Heuristics**: Reading other players, identifying patterns, building alliances, and coordinating with teammates.\n"
    "   - 1: Completely isolated, ignored teammate coordination, or misread clear social signals.\n"
    "   - 5: Orchestrated group voting, formed strong alliances, or coordinated perfectly with faction partners.\n\n"
    "Provide highly detailed, objective written feedback (at least 2-3 sentences) for each player, summarizing their major strategic successes and failures.\n"
    "You must respond with a JSON object containing an 'evaluations' array matching the requested schema."
)


class SeatEvaluation(BaseModel):
    public_player_id: str = Field(description="The public player ID, e.g. P01")
    persuasion_score: int = Field(description="Persuasion score from 1 to 5", ge=1, le=5)
    deception_score: int = Field(description="Deception score from 1 to 5", ge=1, le=5)
    logical_consistency_score: int = Field(
        description="Logical consistency score from 1 to 5", ge=1, le=5
    )
    social_heuristics_score: int = Field(
        description="Social heuristics score from 1 to 5", ge=1, le=5
    )
    written_feedback: str = Field(description="Objective strategic feedback for the player")


class JudgeResponse(BaseModel):
    evaluations: list[SeatEvaluation]


class JudgeAdapter(Protocol):
    """Protocol for custom LLM judge adapters used in tests."""

    async def complete_judge(self, prompt: str) -> str: ...


def build_transcripts(events: list[Any], seats: list[GameSeat]) -> str:
    """Compile seats list and events history into a clear readable transcript for the judge."""
    seats_info = "\n".join(
        [f"- Player {s.public_player_id}: Role={s.role}, Faction={s.faction}" for s in seats]
    )

    lines = []
    lines.append("=== GAME PLAYERS ===")
    lines.append(seats_info)
    lines.append("")
    lines.append("=== CHRONOLOGICAL GAME LOG ===")

    for ev in events:
        pld = ev.payload
        etype = ev.event_type
        phase = ev.phase
        actor = ev.actor_player_id

        if etype == "PhaseStarted":
            lines.append(
                f"\n--- Phase started: {pld.get('phase_kind', phase)} (Day {pld.get('day', 0)}, Round {pld.get('round', 0)}) ---"
            )
        elif etype == "PublicMessageSubmitted":
            lines.append(f"[{phase}] {actor}: {pld.get('text')}")
        elif etype == "PrivateMessageSubmitted":
            chan = pld.get("channel_id", "PRIVATE")
            lines.append(f"[{phase}] (Private {chan}) {actor}: {pld.get('text')}")
        elif etype == "VoteSubmitted":
            target = pld.get("target")
            action_str = f"voted for {target}" if target else "abstained"
            lines.append(f"[{phase}] {actor} {action_str}")
        elif etype == "MafiaKillVoteSubmitted":
            target = pld.get("target")
            lines.append(f"[{phase}] (Mafia Kill Vote) {actor} voted to kill {target}")
        elif etype == "ProtectSubmitted":
            target = pld.get("target")
            lines.append(f"[{phase}] (Doctor Protect) {actor} targeted {target}")
        elif etype == "InvestigateSubmitted":
            target = pld.get("target")
            lines.append(f"[{phase}] (Detective Investigate) {actor} targeted {target}")
        elif etype == "DetectiveResultDelivered":
            target = pld.get("target")
            finding = pld.get("finding")
            lines.append(f"[{phase}] (Detective Result) {actor} found {target} to be {finding}")
        elif etype == "PlayerEliminated":
            lines.append(
                f"\n*** SYSTEM: {pld.get('public_player_id')} was ELIMINATED ({pld.get('role')}, {pld.get('faction')}) due to {pld.get('cause')} ***"
            )
        elif etype == "GameTerminated":
            lines.append(
                f"\n=== GAME OVER: Winner={pld.get('winner')}, Reason={pld.get('reason')} ==="
            )

    return "\n".join(lines)


def resolve_judge_credentials(model: str, settings: Settings) -> tuple[str | None, str | None]:
    """Eagerly resolve credentials for the LLM judge based on model naming."""

    def _resolve(val: str | None) -> str | None:
        if not val:
            return None
        try:
            return resolve_secret(val)
        except Exception:
            return val

    if "xiaomi" in model:
        key = _resolve(settings.xiaomi_api_key or os.environ.get("XIAOMI_API_KEY"))
        return key, settings.xiaomi_base_url
    elif "cerebras" in model:
        key = _resolve(settings.cerebras_api_key or os.environ.get("CEREBRAS_API_KEY"))
        return key, None
    elif "deepinfra" in model:
        key = _resolve(settings.deepinfra_api_key or os.environ.get("DEEPINFRA_API_KEY"))
        return key, None
    elif "zai" in model or "glm" in model:
        key = _resolve(settings.zai_api_key or os.environ.get("ZAI_API_KEY"))
        return key, settings.padrino_zai_api_base

    for k in (
        settings.xiaomi_api_key,
        settings.cerebras_api_key,
        settings.deepinfra_api_key,
        settings.zai_api_key,
    ):
        if k:
            return _resolve(k), None

    return None, None


async def evaluate_completed_game_behavioral(
    session: AsyncSession,
    game_id: uuid.UUID,
    judge_adapter: JudgeAdapter | None = None,
) -> list[BehavioralEvaluation]:
    """Compile the completed game events transcript, execute LLM judge scoring, and persist scores."""
    game = await session.get(Game, game_id)
    if game is None:
        raise ValueError(f"Game {game_id} not found")
    if game.status != "COMPLETED":
        raise ValueError(f"Game {game_id} is not in COMPLETED status (status={game.status})")

    # Fetch game seats and events
    seats = await list_seats(session, game_id)
    events = await list_events(session, game_id)

    transcript = build_transcripts(events, seats)

    user_prompt = f"Here is the game transcript for evaluation:\n\n{transcript}"

    raw_response: str
    if judge_adapter is not None:
        raw_response = await judge_adapter.complete_judge(user_prompt)
    else:
        settings = get_settings()
        model = settings.padrino_behavioral_judge_model
        api_key, api_base = resolve_judge_credentials(model, settings)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_JUDGE_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": JudgeResponse,
            "timeout": 60,
        }
        if api_key is not None:
            kwargs["api_key"] = api_key
        if api_base is not None:
            kwargs["api_base"] = api_base

        response = await litellm.acompletion(**kwargs)
        choices = getattr(response, "choices", None)
        if not choices:
            raise RuntimeError("LLM judge returned empty choices")
        raw_response = choices[0].message.content or ""

    # Parse and validate response
    # Strip markdown fences if present
    stripped = raw_response.strip()
    if stripped.startswith("```"):
        newline = stripped.find("\n")
        if newline != -1:
            body = stripped[newline + 1 :]
            end = body.rfind("```")
            if end != -1:
                stripped = body[:end].strip()

    try:
        data = json.loads(stripped)
        parsed = JudgeResponse.model_validate(data)
    except Exception as exc:
        raise ValueError(
            f"Failed to parse LLM judge response JSON: {exc}. Raw response: {raw_response}"
        ) from exc

    evals_by_player = {e.public_player_id: e for e in parsed.evaluations}

    persisted: list[BehavioralEvaluation] = []

    # Verify that a previous evaluation doesn't already exist to respect uniqueness
    existing_stmt = select(BehavioralEvaluation).where(BehavioralEvaluation.game_id == game_id)
    existing_rows = (await session.execute(existing_stmt)).scalars().all()
    existing_player_ids = {r.public_player_id for r in existing_rows}

    for seat in seats:
        if seat.public_player_id in existing_player_ids:
            continue

        evaluation = evals_by_player.get(seat.public_player_id)
        if evaluation is None:
            # Provide safe default if LLM missed a player
            evaluation = SeatEvaluation(
                public_player_id=seat.public_player_id,
                persuasion_score=3,
                deception_score=3,
                logical_consistency_score=3,
                social_heuristics_score=3,
                written_feedback="No specific feedback provided by judge.",
            )

        obj = BehavioralEvaluation(
            game_id=game_id,
            agent_build_id=seat.agent_build_id,
            public_player_id=seat.public_player_id,
            persuasion_score=evaluation.persuasion_score,
            deception_score=evaluation.deception_score,
            logical_consistency_score=evaluation.logical_consistency_score,
            social_heuristics_score=evaluation.social_heuristics_score,
            written_feedback=evaluation.written_feedback,
        )
        session.add(obj)
        persisted.append(obj)

    await session.flush()
    return persisted


async def run_pending_behavioral_evaluations(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
) -> None:
    """Find completed games missing behavioral evaluation and process them."""
    import structlog

    from padrino.db.base import session_scope

    logger = structlog.get_logger("padrino.behavioral_evaluation")

    async with session_factory() as session:
        subq = select(BehavioralEvaluation.game_id).distinct()
        stmt = (
            select(Game.id)
            .where(Game.status == "COMPLETED", ~Game.id.in_(subq))
            .order_by(Game.completed_at)
            .limit(3)
        )
        result = await session.execute(stmt)
        game_ids = list(result.scalars())

    for game_id in game_ids:
        try:
            async with session_scope(session_factory) as session:
                await evaluate_completed_game_behavioral(session, game_id)
            logger.info("behavioral_evaluation.game.success", game_id=str(game_id))
        except Exception as exc:
            logger.error("behavioral_evaluation.game.failed", game_id=str(game_id), error=str(exc))
