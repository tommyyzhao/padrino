"""Real-provider integration test for US-079's same-model multi-host fallback.

Marked ``@pytest.mark.integration`` so the default ``pytest -m "not integration"``
sweep skips it. Invoke explicitly with ``uv run pytest -m integration -k
same_model_fallback`` while ``CEREBRAS_API_KEY`` and ``ZAI_API_KEY`` are set.

Two scenarios:

1. **Happy path** — the primary host (Cerebras GLM-4.7) serves the call
   cleanly. The adapter never touches the Z.AI fallback, status==``ok``.
   This proves the same-model host *configuration* doesn't break the normal
   routing path.

2. **Forced fallback** — the primary host is monkeypatched to raise a
   ``RateLimitError`` on every attempt, exhausting its retries. The adapter
   must route to the Z.AI GLM-4.7 endpoint and produce a valid
   :class:`AgentResponse` with status==``same_model_fallback_ok``. This is
   the only way to deterministically trigger the cross-host path in CI;
   waiting for a real Cerebras 429 is unreliable.

Both scenarios share a single canonical Day-1 observation.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from dotenv import load_dotenv
from litellm.exceptions import RateLimitError

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import Faction, PhaseKind, Role
from padrino.core.observations import Observation, build_observation
from padrino.core.rulesets import mini7_v1
from padrino.llm.adapter import AgentBuild, RoutingPolicy, SameModelHost
from padrino.llm.litellm_adapter import LiteLlmAdapter
from padrino.settings import Settings


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


def _observation() -> Observation:
    phase = Phase(kind=PhaseKind.NIGHT_0_MAFIA_INTRO, day=0, round=0)
    state = GameState(
        ruleset_id=mini7_v1.RULESET_ID,
        game_id="G-INTEGRATION-SAME-MODEL-001",
        game_seed="same-model-fallback-001",
        current_phase=phase,
        seats=_SEATS,
        day=phase.day,
    )
    return build_observation(state, _SEATS[0], EventLog(), mini7_v1)


def _build_adapter() -> LiteLlmAdapter:
    settings = Settings()
    policy = RoutingPolicy(
        primary_model=settings.padrino_primary_model,
        fallback_model=None,  # isolate the same-model path; no different-model bailout
        same_model_hosts=(
            SameModelHost(
                provider="zai",
                litellm_model_id="openai/glm-4.7",
                api_base=settings.padrino_zai_api_base,
                auth_secret_ref="env:ZAI_API_KEY",
            ),
        ),
    )
    build = AgentBuild(
        provider="cerebras",
        model_id=settings.padrino_primary_model,
        prompt_version="default",
        inference_params={
            "temperature": settings.padrino_temperature,
            "top_p": settings.padrino_top_p,
        },
        adapter_version="litellm-v1",
    )
    return LiteLlmAdapter(
        routing_policy=policy,
        agent_build=build,
        timeout_s=float(settings.padrino_llm_timeout_seconds),
        auth_secret_ref="env:CEREBRAS_API_KEY",
    )


@pytest.fixture
def _require_keys() -> None:
    load_dotenv(override=False)
    missing = [name for name in ("CEREBRAS_API_KEY", "ZAI_API_KEY") if not os.environ.get(name)]
    if missing:
        pytest.skip(f"integration keys not set: {', '.join(missing)}")


@pytest.mark.integration
async def test_primary_alone_serves_when_healthy(_require_keys: None) -> None:
    """When the primary host succeeds, the adapter never reaches the Z.AI host."""
    adapter = _build_adapter()
    result = await adapter.complete(_observation())
    assert result.status == "ok", (
        f"expected primary host to serve cleanly; got status={result.status} "
        f"raw={result.raw_response[:200]!r} error={result.error!r}"
    )
    assert isinstance(result.parsed_response, AgentResponse)
    # last_attempts records ONE row: the successful primary attempt. The
    # Z.AI host should never have been called.
    assert len(adapter.last_attempts) == 1


@pytest.mark.integration
async def test_cerebras_429_routes_to_zai_glm47(_require_keys: None) -> None:
    """A monkeypatched 429 on the Cerebras primary routes the call to Z.AI."""
    adapter = _build_adapter()
    real_acompletion = __import__("litellm").acompletion

    async def fake_acompletion(*args: object, **kwargs: object) -> object:
        model = kwargs.get("model")
        if model == "cerebras/zai-glm-4.7":
            raise RateLimitError(
                message="forced 429 to trigger same-model fallback",
                llm_provider="cerebras",
                model="cerebras/zai-glm-4.7",
            )
        # Anything else (the Z.AI same-model host) goes through to the real
        # endpoint so we exercise the cross-host path end-to-end.
        return await real_acompletion(*args, **kwargs)

    with patch(
        "padrino.llm.litellm_adapter.litellm.acompletion",
        new=AsyncMock(side_effect=fake_acompletion),
    ):
        result = await adapter.complete(_observation())

    assert result.status == "same_model_fallback_ok", (
        f"expected same-model fallback to succeed; got status={result.status} "
        f"error={result.error!r}"
    )
    assert isinstance(result.parsed_response, AgentResponse)
    # last_attempts: primary attempt(s) (demoted to primary_failed) + the
    # successful same-model attempt.
    assert adapter.last_attempts[-1].status == "same_model_fallback_ok"
    primary_attempts = [a for a in adapter.last_attempts if a.status == "primary_failed"]
    assert primary_attempts, "expected at least one primary_failed attempt to be recorded"
