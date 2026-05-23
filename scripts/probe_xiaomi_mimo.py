"""Probe Xiaomi Mimo token-plan responses for Padrino's agent contract."""

from __future__ import annotations

import argparse
import asyncio

from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import Faction, PhaseKind, Role
from padrino.core.observations import Observation, build_observation
from padrino.core.rulesets import mini7_v1
from padrino.llm.adapter import AgentBuild, RoutingPolicy
from padrino.llm.litellm_adapter import LiteLlmAdapter
from padrino.llm.secrets import SecretResolutionError
from padrino.settings import Settings

_MODELS = ("mimo-v2.5", "mimo-v2.5-pro")


def _seat(pid: str, idx: int, role: Role, faction: Faction) -> Seat:
    return Seat(
        public_player_id=pid,
        seat_index=idx,
        role=role,
        faction=faction,
        alive=True,
    )


_SEATS: tuple[Seat, ...] = (
    _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
    _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
    _seat("P03", 2, Role.DETECTIVE, Faction.TOWN),
    _seat("P04", 3, Role.DOCTOR, Faction.TOWN),
    _seat("P05", 4, Role.VILLAGER, Faction.TOWN),
    _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
    _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
)


def _canonical_observation() -> Observation:
    phase = Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)
    state = GameState(
        ruleset_id=mini7_v1.RULESET_ID,
        game_id="G-XIAOMI-PROBE",
        game_seed="seed-xiaomi-probe",
        current_phase=phase,
        seats=_SEATS,
        day=phase.day,
    )
    return build_observation(state, _SEATS[0], EventLog(), mini7_v1)


async def _run(model_name: str) -> None:
    settings = Settings()
    litellm_model_id = f"openai/{model_name}"
    adapter = LiteLlmAdapter(
        routing_policy=RoutingPolicy(primary_model=litellm_model_id, fallback_model=None),
        agent_build=AgentBuild(
            provider="xiaomi",
            model_id=litellm_model_id,
            prompt_version="probe_mini7_v1",
            inference_params={
                "temperature": settings.padrino_temperature,
                "top_p": settings.padrino_top_p,
            },
            adapter_version="litellm-probe",
        ),
        timeout_s=float(settings.padrino_llm_timeout_seconds),
        auth_secret_ref="env:XIAOMI_API_KEY",
        api_base=settings.xiaomi_base_url,
    )
    result = await adapter.complete(_canonical_observation())
    print(result.raw_response)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_name", choices=_MODELS)
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.model_name))
    except SecretResolutionError as exc:
        raise SystemExit(f"XIAOMI_API_KEY is not configured: {exc}") from exc


if __name__ == "__main__":
    main()
