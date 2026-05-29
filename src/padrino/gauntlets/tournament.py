"""Multi-game heterogeneous tournament runner (US-084).

Plays the same heterogeneous roster across N games, rotating each roster slot
through different seats per game via the pure-core
:func:`padrino.core.seating.seat_permutation`. Because roles are assigned by
seat index, the permutation spreads every model across town and mafia roles,
which tightens the per-faction Wilson confidence intervals that
:func:`padrino.gauntlets.evaluation.evaluate_gauntlet` reports.

The ``n_games == 1`` path is identical to running a single heterogeneous game
(US-083): ``seat_permutation(seed, 7)`` for game index 0 is just a permutation,
and the runner threads it through exactly as US-083 did.

Impure ``gauntlets`` layer; pure-core never imports it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.rulesets import mini7_v1
from padrino.core.seating import seat_permutation
from padrino.gauntlets.heterogeneous import build_heterogeneous_adapter
from padrino.gauntlets.scheduler import create_gauntlet, derive_game_seed
from padrino.llm.adapter import AgentBuild, LlmAdapter
from padrino.runner.game_runner import GameConfig, GameOutcome, GamePersistence, run_game
from padrino.settings import Settings

_logger = structlog.get_logger("padrino.gauntlets.tournament")

# Builds a game adapter from a (already-permuted) seat -> AgentBuild mapping.
# Injectable so tests can substitute a mock adapter for the real provider path.
AdapterFactory = Callable[[Mapping[str, AgentBuild]], LlmAdapter]


@dataclass(frozen=True, slots=True)
class TournamentResult:
    outcomes: tuple[GameOutcome, ...]
    games_run: int
    total_cost_usd: float
    cost_capped: bool


def _seat_ids(player_count: int) -> list[str]:
    return [f"P{i + 1:02d}" for i in range(player_count)]


async def run_heterogeneous_tournament(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    league_id: uuid.UUID,
    gauntlet_seed: str,
    game_ids: Sequence[uuid.UUID],
    base_agent_builds_by_seat: Mapping[str, uuid.UUID],
    base_agent_build_assignments: Mapping[str, AgentBuild],
    settings: Settings,
    cost_cap_usd: float | None = None,
    adapter_factory: AdapterFactory | None = None,
) -> TournamentResult:
    """Run one game per entry in ``game_ids`` with a per-game seat permutation.

    ``base_agent_builds_by_seat`` (seat -> DB agent_build id, for ranking
    attribution) and ``base_agent_build_assignments`` (seat -> AgentBuild value
    object, for adapter construction) describe the canonical P01..P07 ordering;
    each game permutes both consistently. Stops early — leaving later games
    unplayed — if cumulative cost crosses ``cost_cap_usd``.
    """
    player_count = mini7_v1.PLAYER_COUNT
    seat_ids = _seat_ids(player_count)
    if set(base_agent_builds_by_seat) != set(seat_ids):
        raise ValueError(f"base_agent_builds_by_seat must cover seats {seat_ids}")
    if set(base_agent_build_assignments) != set(seat_ids):
        raise ValueError(f"base_agent_build_assignments must cover seats {seat_ids}")

    factory: AdapterFactory = (
        adapter_factory
        if adapter_factory is not None
        else (lambda assignments: build_heterogeneous_adapter(assignments, settings=settings))
    )

    outcomes: list[GameOutcome] = []
    total_cost = 0.0
    cost_capped = False
    for game_index, game_id in enumerate(game_ids):
        perm = seat_permutation(f"{gauntlet_seed}:{game_index}", player_count)
        permuted_builds = {
            seat_ids[i]: base_agent_builds_by_seat[seat_ids[perm[i]]] for i in range(player_count)
        }
        permuted_assignments = {
            seat_ids[i]: base_agent_build_assignments[seat_ids[perm[i]]]
            for i in range(player_count)
        }
        adapter = factory(permuted_assignments)
        config = GameConfig(
            game_id=str(game_id),
            game_seed=derive_game_seed(gauntlet_seed, game_index),
            ruleset_id=mini7_v1.RULESET_ID,
            timeout_s=float(settings.padrino_llm_timeout_seconds),
        )
        persistence = GamePersistence(
            session_factory=session_factory,
            game_id=game_id,
            agent_builds=permuted_builds,
            league_id=league_id,
        )
        outcome = await run_game(config, adapter, ranked=True, persistence=persistence)
        outcomes.append(outcome)
        total_cost += sum((c.cost_usd or 0.0) for c in outcome.llm_calls)
        _logger.info(
            "tournament.game.completed",
            game_index=game_index,
            winner=outcome.final_state.terminal_result,
            cumulative_cost_usd=round(total_cost, 4),
        )
        if cost_cap_usd is not None and total_cost > cost_cap_usd:
            cost_capped = True
            _logger.warning(
                "tournament.cost_capped",
                cumulative_cost_usd=round(total_cost, 4),
                cost_cap_usd=cost_cap_usd,
            )
            break

    return TournamentResult(
        outcomes=tuple(outcomes),
        games_run=len(outcomes),
        total_cost_usd=total_cost,
        cost_capped=cost_capped,
    )


async def project_agent_build(session: AsyncSession, agent_build_id: uuid.UUID) -> AgentBuild:
    """Project a DB ``agent_build`` row chain into an :class:`AgentBuild` value object.

    Flattens ``agent_build -> model_config -> model_provider`` (and the prompt
    version row) into the pure value object the adapter consumes. Inference
    params layer the model_config defaults under the agent_build overrides.
    """
    from padrino.db.models import AgentBuild as AgentBuildRow
    from padrino.db.models import ModelConfig, ModelProvider, PromptVersion

    ab = await session.get(AgentBuildRow, agent_build_id)
    if ab is None:
        raise ValueError(f"agent_build {agent_build_id} not found")
    mc = await session.get(ModelConfig, ab.model_config_id)
    if mc is None:
        raise ValueError(f"model_config {ab.model_config_id} not found")
    provider = await session.get(ModelProvider, mc.provider_id)
    if provider is None:
        raise ValueError(f"model_provider {mc.provider_id} not found")
    pv = await session.get(PromptVersion, ab.prompt_version_id)
    if pv is None:
        raise ValueError(f"prompt_version {ab.prompt_version_id} not found")

    inference: dict[str, object] = {
        "temperature": mc.default_temperature,
        "top_p": mc.default_top_p,
    }
    inference.update(ab.inference_params or {})
    return AgentBuild(
        provider=provider.name,
        model_id=mc.litellm_model_id or mc.model_name,
        prompt_version=pv.version,
        inference_params=inference,
        adapter_version=ab.adapter_version,
    )


async def run_tournament_from_roster(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    league_id: uuid.UUID,
    gauntlet_seed: str,
    roster_by_seat: Mapping[str, uuid.UUID],
    n_games: int,
    settings: Settings,
    cost_cap_usd: float | None = None,
    adapter_factory: AdapterFactory | None = None,
) -> tuple[uuid.UUID, TournamentResult]:
    """Create a gauntlet from a seat -> agent_build_id roster and run N games.

    The prompt-version FK for the gauntlet is taken from the first seat's
    agent_build. Returns ``(gauntlet_id, result)``.
    """
    seat_ids = _seat_ids(mini7_v1.PLAYER_COUNT)
    if set(roster_by_seat) != set(seat_ids):
        raise ValueError(f"roster must cover exactly seats {seat_ids}")

    from padrino.db.models import AgentBuild as AgentBuildRow

    async with session_factory() as session:
        assignments = {
            seat: await project_agent_build(session, build_id)
            for seat, build_id in roster_by_seat.items()
        }
        first = await session.get(AgentBuildRow, roster_by_seat[seat_ids[0]])
        if first is None:
            raise ValueError(f"agent_build {roster_by_seat[seat_ids[0]]} not found")
        prompt_version_id = first.prompt_version_id

    roster = [roster_by_seat[seat] for seat in seat_ids]
    async with session_factory() as session:
        created = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=prompt_version_id,
            clone_count=n_games,
            gauntlet_seed=gauntlet_seed,
            roster=roster,
        )

    result = await run_heterogeneous_tournament(
        session_factory=session_factory,
        league_id=league_id,
        gauntlet_seed=gauntlet_seed,
        game_ids=created.game_ids,
        base_agent_builds_by_seat=dict(roster_by_seat),
        base_agent_build_assignments=assignments,
        settings=settings,
        cost_cap_usd=cost_cap_usd,
        adapter_factory=adapter_factory,
    )
    return created.gauntlet_id, result


__all__ = [
    "AdapterFactory",
    "TournamentResult",
    "project_agent_build",
    "run_heterogeneous_tournament",
    "run_tournament_from_roster",
]
