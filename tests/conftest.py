"""Shared pytest fixtures and scripted-agent helpers.

The helpers below assemble ``dict[tuple[str, str], AgentResponse]`` scripts
keyed by ``(phase_id, public_player_id)`` — the shape consumed by
:class:`padrino.llm.mock.DeterministicMockAdapter`. Integration tests
(US-027+) compose these to drive complete games without a real LLM.

This module also installs a ``pytest_collection_modifyitems`` hook that
deselects the ``live_llm`` marker by default. The recorded-cassette contract
suite under ``tests/llm/test_litellm_contract.py`` (US-051) opts in via
``-m live_llm`` or the ``--live-llm`` flag.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.enums import ActionType
from padrino.core.rulesets import mini7_v1


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live-llm",
        action="store_true",
        default=False,
        help="run recorded-cassette live LLM contract tests (US-051)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Default-skip ``live_llm`` items unless explicitly opted in.

    Opt-in paths:
    * ``--live-llm`` flag on the CLI, or
    * ``-m live_llm`` marker selector (the recorded-cassette CI job).
    """

    if config.getoption("--live-llm"):
        return
    markexpr = (config.option.markexpr or "").strip()
    if "live_llm" in markexpr and "not live_llm" not in markexpr:
        return
    skip = pytest.mark.skip(
        reason="live_llm cassette tests are opt-in; pass --live-llm or '-m live_llm'"
    )
    for item in items:
        if "live_llm" in item.keywords:
            item.add_marker(skip)


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
        return _response(ActionType.ABSTAIN, None)
    return _response(ActionType.NOOP, None)


def mini7_phase_ids() -> tuple[str, ...]:
    """All phase ids mini7_v1 may emit, in chronological order.

    Excludes ``SETUP`` and ``TERMINAL`` since those phases never prompt seats.
    """
    out: list[str] = ["NIGHT_0_MAFIA_INTRO"]
    for d in range(1, mini7_v1.MAX_DAYS + 1):
        for r in range(1, mini7_v1.DISCUSSION_ROUNDS_PER_DAY + 1):
            out.append(f"DAY_{d}_DISCUSSION_ROUND_{r}")
        out.append(f"DAY_{d}_VOTE")
        out.append(f"NIGHT_{d}_MAFIA_DISCUSSION")
        out.append(f"NIGHT_{d}_ACTIONS")
    return tuple(out)


def make_villager_script(
    seat_ids: Sequence[str],
    phase_ids: Sequence[str],
    *,
    votes: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[tuple[str, str], AgentResponse]:
    """Build a NOOP/ABSTAIN baseline for ``seat_ids`` across ``phase_ids``.

    ``votes[phase_id][seat_id] = target`` upgrades that single seat's vote-phase
    response from ABSTAIN to ``VOTE(target)``.
    """
    overrides: Mapping[str, Mapping[str, str]] = votes or {}
    script: dict[tuple[str, str], AgentResponse] = {}
    for phase_id in phase_ids:
        phase_overrides = overrides.get(phase_id, {})
        for sid in seat_ids:
            if sid in phase_overrides:
                script[(phase_id, sid)] = _response(ActionType.VOTE, phase_overrides[sid])
            else:
                script[(phase_id, sid)] = _phase_default(phase_id)
    return script


def make_mafia_script(
    mafia_ids: Sequence[str],
    phase_ids: Sequence[str],
    *,
    night_kill_targets: Mapping[str, str] | None = None,
    votes: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[tuple[str, str], AgentResponse]:
    """Build a mafia-side script.

    ``night_kill_targets[phase_id] = target`` makes every mafia seat submit
    ``MAFIA_KILL(target)`` on that night-actions phase. Outside listed kill
    phases each mafia seat defaults to NOOP (or ABSTAIN on day votes). The
    ``votes`` override mirrors :func:`make_villager_script`.
    """
    kills: Mapping[str, str] = night_kill_targets or {}
    vote_overrides: Mapping[str, Mapping[str, str]] = votes or {}
    script: dict[tuple[str, str], AgentResponse] = {}
    for phase_id in phase_ids:
        phase_votes = vote_overrides.get(phase_id, {})
        for sid in mafia_ids:
            if sid in phase_votes:
                script[(phase_id, sid)] = _response(ActionType.VOTE, phase_votes[sid])
            elif phase_id in kills and phase_id.endswith("_ACTIONS"):
                script[(phase_id, sid)] = _response(ActionType.MAFIA_KILL, kills[phase_id])
            else:
                script[(phase_id, sid)] = _phase_default(phase_id)
    return script


def make_town_win_script(
    *,
    mafia_ids: Sequence[str],
    town_ids: Sequence[str],
    doctor_id: str,
    detective_id: str,
) -> dict[tuple[str, str], AgentResponse]:
    """Full mini7_v1 script that resolves to a TOWN win.

    Strategy: D1 vote eliminates ``mafia_ids[0]``; on N1 the doctor protects
    the surviving mafia's target so no kill lands; D2 vote eliminates
    ``mafia_ids[1]`` and the engine terminates with ``winner == 'TOWN'``.
    """
    if len(mafia_ids) < 2:
        raise ValueError("mini7_v1 has exactly 2 mafia seats")
    if doctor_id not in town_ids or detective_id not in town_ids:
        raise ValueError("doctor_id and detective_id must appear in town_ids")

    phase_ids = mini7_phase_ids()
    all_seats = list(mafia_ids) + list(town_ids)
    script: dict[tuple[str, str], AgentResponse] = {
        (p, s): _phase_default(p) for p in phase_ids for s in all_seats
    }

    for sid in town_ids:
        script[("DAY_1_VOTE", sid)] = _response(ActionType.VOTE, mafia_ids[0])

    protect_target = next(t for t in town_ids if t != doctor_id)
    for mid in mafia_ids:
        script[("NIGHT_1_ACTIONS", mid)] = _response(ActionType.MAFIA_KILL, protect_target)
    script[("NIGHT_1_ACTIONS", doctor_id)] = _response(ActionType.PROTECT, protect_target)
    script[("NIGHT_1_ACTIONS", detective_id)] = _response(ActionType.INVESTIGATE, mafia_ids[1])

    for sid in town_ids:
        script[("DAY_2_VOTE", sid)] = _response(ActionType.VOTE, mafia_ids[1])

    return script


def make_mafia_win_script(
    *,
    mafia_ids: Sequence[str],
    town_ids: Sequence[str],
) -> dict[tuple[str, str], AgentResponse]:
    """Full mini7_v1 script that resolves to a MAFIA win.

    Strategy: town abstains every day vote; mafia kills one town seat per
    night for three consecutive nights → parity at day 4 → ``winner == 'MAFIA'``.
    """
    if len(mafia_ids) < 2 or len(town_ids) < 3:
        raise ValueError("mini7_v1 expects 2 mafia and 5 town seats")

    phase_ids = mini7_phase_ids()
    all_seats = list(mafia_ids) + list(town_ids)
    script: dict[tuple[str, str], AgentResponse] = {
        (p, s): _phase_default(p) for p in phase_ids for s in all_seats
    }

    night_targets = {
        "NIGHT_1_ACTIONS": town_ids[0],
        "NIGHT_2_ACTIONS": town_ids[1],
        "NIGHT_3_ACTIONS": town_ids[2],
    }
    for phase_id, target in night_targets.items():
        for mid in mafia_ids:
            script[(phase_id, mid)] = _response(ActionType.MAFIA_KILL, target)

    return script


__all__ = [
    "make_mafia_script",
    "make_mafia_win_script",
    "make_town_win_script",
    "make_villager_script",
    "mini7_phase_ids",
]
