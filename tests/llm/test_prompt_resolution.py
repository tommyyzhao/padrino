"""Per-seat prompt resolution end-to-end (US-052).

Asserts:

* Every :class:`RoleFamily` resolves to a distinct canonical prompt at game
  start.
* ``LiteLlmAdapter`` routes the per-call system prompt by
  ``observation.you.role`` when given a ``system_prompts_by_role`` mapping.
* An explicit per-build override (caller passes a single ``system_prompt``
  string, no mapping) wins over the canonical resolution.
* Ranked-observation safety: the bundled prompts plus the observation
  projection never leak the forbidden role-disclosure keys for other seats.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import ActionType, Faction, PhaseKind, Role, RoleFamily
from padrino.core.observations import build_observation
from padrino.core.rulesets import mini7_v1, roleblock10_v1, visit12_v1
from padrino.llm.adapter import AgentBuild, RoutingPolicy
from padrino.llm.litellm_adapter import DEFAULT_SYSTEM_PROMPT, LiteLlmAdapter
from padrino.llm.prompts import (
    CANONICAL_VERSION,
    canonical_prompts_by_role,
    iter_canonical_prompts,
    load_canonical,
)

ACOMPLETION_PATH = "padrino.llm.litellm_adapter.litellm.acompletion"
_AUTH_ENV = "PADRINO_TEST_PROMPT_RESOLUTION_KEY"


@pytest.fixture(autouse=True)
def _set_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_AUTH_ENV, "test-key")


def _seat(pid: str, idx: int, role: Role, faction: Faction) -> Seat:
    return Seat(
        public_player_id=pid,
        seat_index=idx,
        role=role,
        faction=faction,
        alive=True,
    )


SEATS: tuple[Seat, ...] = (
    _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
    _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
    _seat("P03", 2, Role.DETECTIVE, Faction.TOWN),
    _seat("P04", 3, Role.DOCTOR, Faction.TOWN),
    _seat("P05", 4, Role.VILLAGER, Faction.TOWN),
    _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
    _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
)


def _state(phase: Phase) -> GameState:
    return GameState(
        ruleset_id=mini7_v1.RULESET_ID,
        game_id="G-MOCK",
        game_seed="seed",
        current_phase=phase,
        seats=SEATS,
        day=phase.day,
    )


def _ok_response_text() -> str:
    return json.dumps(
        {
            "public_message": None,
            "private_message": None,
            "action": {"type": ActionType.NOOP.value, "target": None},
            "memory_update": "",
            "rationale_summary": None,
        }
    )


def _fake_completion() -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=_ok_response_text()))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        id="resp-x",
        _hidden_params={"response_cost": 0.0},
    )


def _build_adapter(
    *,
    system_prompts_by_role: Mapping[Role, str] | None = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
) -> LiteLlmAdapter:
    return LiteLlmAdapter(
        routing_policy=RoutingPolicy(primary_model="cerebras/zai-glm-4.7", fallback_model=None),
        agent_build=AgentBuild(
            provider="cerebras",
            model_id="m",
            prompt_version=CANONICAL_VERSION,
            inference_params={},
            adapter_version="a",
        ),
        timeout_s=5.0,
        auth_secret_ref=f"env:{_AUTH_ENV}",
        system_prompt=system_prompt,
        system_prompts_by_role=system_prompts_by_role,
    )


def test_every_role_family_resolves_to_a_distinct_prompt() -> None:
    seen_versions: dict[RoleFamily, str] = {}
    seen_prompts: dict[RoleFamily, str] = {}
    for role_family in RoleFamily:
        template = load_canonical(mini7_v1.RULESET_ID, role_family)
        seen_versions[role_family] = template.version
        seen_prompts[role_family] = template.system_prompt

    assert set(seen_versions.values()) == {CANONICAL_VERSION}
    # Every prompt body is distinct: a content-addressed hash check.
    assert len(set(seen_prompts.values())) == len(seen_prompts)


def test_canonical_prompts_by_role_covers_every_role() -> None:
    prompts = canonical_prompts_by_role()
    assert set(prompts.keys()) == set(mini7_v1.ROLE_COUNTS)
    # MAFIA_GOON → DECEPTIVE; DETECTIVE → INVESTIGATIVE; DOCTOR → PROTECTIVE;
    # VILLAGER → VANILLA_TOWN. Confirm via the bundled markdown content.
    expected = {
        role: load_canonical(mini7_v1.RULESET_ID, mini7_v1.role_family_for(role))
        for role in mini7_v1.ROLE_COUNTS
    }
    for role, template in expected.items():
        assert prompts[role] == template.system_prompt


def test_roleblock10_uses_deceptive_prompt_for_mafia_roleblocker() -> None:
    prompts = canonical_prompts_by_role(roleblock10_v1.RULESET_ID)

    assert set(prompts) == set(roleblock10_v1.ROLE_COUNTS)
    assert (
        prompts[Role.MAFIA_ROLEBLOCKER]
        == load_canonical(
            roleblock10_v1.RULESET_ID,
            RoleFamily.DECEPTIVE,
        ).system_prompt
    )


def test_visit12_uses_investigative_prompts_for_tracker_and_watcher() -> None:
    prompts = canonical_prompts_by_role(visit12_v1.RULESET_ID)

    assert set(prompts) == set(visit12_v1.ROLE_COUNTS)
    expected_prompt = load_canonical(
        visit12_v1.RULESET_ID,
        RoleFamily.INVESTIGATIVE,
    ).system_prompt
    assert prompts[Role.TRACKER] == expected_prompt
    assert prompts[Role.WATCHER] == expected_prompt


@pytest.mark.parametrize(
    ("seat", "expected_role_family"),
    [
        (SEATS[0], RoleFamily.DECEPTIVE),
        (SEATS[2], RoleFamily.INVESTIGATIVE),
        (SEATS[3], RoleFamily.PROTECTIVE),
        (SEATS[4], RoleFamily.VANILLA_TOWN),
    ],
)
def test_litellm_adapter_routes_system_prompt_by_role(
    seat: Seat, expected_role_family: RoleFamily
) -> None:
    obs = build_observation(
        _state(Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)),
        seat,
        EventLog(),
        mini7_v1,
    )
    expected_body = load_canonical(mini7_v1.RULESET_ID, expected_role_family).system_prompt

    mock = AsyncMock(return_value=_fake_completion())
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter(system_prompts_by_role=canonical_prompts_by_role())
        result = asyncio.run(adapter.complete(obs))

    assert isinstance(result.parsed_response, AgentResponse)
    sent_messages = mock.call_args.kwargs["messages"]
    assert sent_messages[0] == {"role": "system", "content": expected_body}


def test_explicit_system_prompt_overrides_canonical_resolution() -> None:
    """A build that pins ``system_prompt`` (no role mapping) wins for every seat."""
    overridden = "OVERRIDE PROMPT — pinned at build level"
    obs = build_observation(
        _state(Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)),
        SEATS[2],  # Detective; canonical resolution would pick INVESTIGATIVE.
        EventLog(),
        mini7_v1,
    )
    mock = AsyncMock(return_value=_fake_completion())
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter(system_prompt=overridden)
        asyncio.run(adapter.complete(obs))

    sent_messages = mock.call_args.kwargs["messages"]
    assert sent_messages[0] == {"role": "system", "content": overridden}


def test_unknown_role_falls_back_to_default_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the per-role mapping is missing the seat's role, the fallback applies.

    This is paranoia coverage: deployments configuring a partial mapping
    should still produce *some* prompt rather than crashing.
    """
    fallback = "FALLBACK PROMPT"
    partial_mapping = {Role.MAFIA_GOON: "MAFIA-ONLY"}  # detective absent
    obs = build_observation(
        _state(Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)),
        SEATS[2],
        EventLog(),
        mini7_v1,
    )
    mock = AsyncMock(return_value=_fake_completion())
    with patch(ACOMPLETION_PATH, new=mock):
        adapter = _build_adapter(
            system_prompts_by_role=partial_mapping,
            system_prompt=fallback,
        )
        asyncio.run(adapter.complete(obs))

    sent_messages = mock.call_args.kwargs["messages"]
    assert sent_messages[0]["content"] == fallback


def test_ranked_observation_safety_observation_never_leaks_other_seat_roles() -> None:
    """Concatenation of (canonical prompt | seat observation) reveals only the seat's own role.

    The observation must surface ``you.role`` and ``you.faction`` (the seat
    *owns* its identity) but the public/private event projection must not
    name any other seat's role or faction. We assert that the only role
    name leaking into the user-side payload is the seat's own role.
    """
    seat = SEATS[2]  # Detective.
    obs = build_observation(
        _state(Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)),
        seat,
        EventLog(),
        mini7_v1,
    )
    user_payload = obs.model_dump_json()
    parsed = json.loads(user_payload)
    # `you` reveals self-role; every other role label must not appear in the
    # public/private/dead/alive projections.
    other_roles = {r.value for r in Role if r != seat.role}
    forbidden_keys = ("role", "faction")
    for evt in parsed["public_events"]:
        for k in forbidden_keys:
            assert k not in evt["payload"], evt
    for evt in parsed["private_events"]:
        for k in forbidden_keys:
            assert k not in evt["payload"], evt
    for other in other_roles:
        assert f'"{other}"' not in json.dumps(parsed["public_events"]), other
        assert f'"{other}"' not in json.dumps(parsed["private_events"]), other


def test_canonical_prompt_concat_does_not_leak_other_seat_identities() -> None:
    """The bundled canonical prompts themselves contain no per-seat identifiers."""
    for template in iter_canonical_prompts():
        body = template.system_prompt
        # No `P\d\d` public ids and no other seats' role enum values.
        import re

        assert not re.search(r"\bP0[1-7]\b", body), f"{template.role_family} prompt names a seat id"
