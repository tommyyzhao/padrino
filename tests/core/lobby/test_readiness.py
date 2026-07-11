"""Pure lobby launch-readiness tests (US-329)."""

from __future__ import annotations

import pytest

from padrino.core.enums import LobbySeatKind
from padrino.core.lobby.readiness import (
    LobbyReadinessMember,
    LobbyReadinessReason,
    LobbyReadinessSeat,
    evaluate_lobby_readiness,
)
from padrino.core.rulesets import BUILTIN_RULESET_IDS, get_ruleset


def _member(member_id: str, *, ready: bool = True, present: bool = True) -> LobbyReadinessMember:
    return LobbyReadinessMember(member_id=member_id, ready=ready, present=present)


def _seat(
    index: int,
    kind: LobbySeatKind | str,
    *,
    member_id: str | None = None,
    agent_build_id: str | None = None,
) -> LobbyReadinessSeat:
    return LobbyReadinessSeat(
        seat_index=index,
        seat_kind=kind,
        member_id=member_id,
        agent_build_id=agent_build_id,
    )


@pytest.mark.parametrize("ruleset_id", BUILTIN_RULESET_IDS)
def test_readiness_allows_every_human_count_for_builtin_player_counts(ruleset_id: str) -> None:
    player_count = get_ruleset(ruleset_id).PLAYER_COUNT
    for human_count in range(1, player_count + 1):
        members = tuple(_member(f"member-{index}") for index in range(human_count))
        seats = tuple(
            _seat(index, LobbySeatKind.HUMAN, member_id=f"member-{index}")
            if index < human_count
            else _seat(index, LobbySeatKind.AI, agent_build_id=f"build-{index}")
            for index in range(player_count)
        )

        decision = evaluate_lobby_readiness(
            members=members,
            seats=seats,
            expected_player_count=player_count,
            available_ai_build_count=0,
        )

        assert decision.allowed is True
        assert decision.reasons == ()


def test_readiness_reports_stable_ordered_reasons_for_malformed_layout() -> None:
    decision = evaluate_lobby_readiness(
        expected_player_count=4,
        available_ai_build_count=0,
        members=(
            _member("unseated"),
            _member("duplicated", ready=False, present=False),
        ),
        seats=(
            _seat(0, LobbySeatKind.HUMAN, member_id="duplicated"),
            _seat(0, LobbySeatKind.HUMAN, member_id="duplicated"),
            _seat(2, LobbySeatKind.HUMAN, member_id="unknown"),
            _seat(3, LobbySeatKind.AI),
        ),
    )

    assert decision.allowed is False
    assert decision.reasons == (
        LobbyReadinessReason.MISSING_SEAT,
        LobbyReadinessReason.DUPLICATE_SEAT,
        LobbyReadinessReason.ORPHAN_SEAT,
        LobbyReadinessReason.ABSENT_MEMBER,
        LobbyReadinessReason.NOT_READY_MEMBER,
        LobbyReadinessReason.UNRESOLVED_AI_SEAT,
        LobbyReadinessReason.COUNT_MISMATCH,
    )


@pytest.mark.parametrize(
    ("members", "seats", "reason"),
    [
        (
            (_member("host"), _member("friend")),
            (
                _seat(0, LobbySeatKind.HUMAN, member_id="host"),
                _seat(1, LobbySeatKind.AI, agent_build_id="build-1"),
            ),
            LobbyReadinessReason.MISSING_SEAT,
        ),
        (
            (_member("host"),),
            (
                _seat(0, LobbySeatKind.HUMAN, member_id="host"),
                _seat(1, LobbySeatKind.HUMAN, member_id="host"),
            ),
            LobbyReadinessReason.DUPLICATE_SEAT,
        ),
        (
            (_member("host"),),
            (
                _seat(0, LobbySeatKind.HUMAN, member_id="host"),
                _seat(1, LobbySeatKind.HUMAN, member_id="ghost"),
            ),
            LobbyReadinessReason.ORPHAN_SEAT,
        ),
        (
            (_member("host", present=False),),
            (
                _seat(0, LobbySeatKind.HUMAN, member_id="host"),
                _seat(1, LobbySeatKind.AI, agent_build_id="build-1"),
            ),
            LobbyReadinessReason.ABSENT_MEMBER,
        ),
        (
            (_member("host", ready=False),),
            (
                _seat(0, LobbySeatKind.HUMAN, member_id="host"),
                _seat(1, LobbySeatKind.AI, agent_build_id="build-1"),
            ),
            LobbyReadinessReason.NOT_READY_MEMBER,
        ),
        (
            (_member("host"),),
            (
                _seat(0, LobbySeatKind.HUMAN, member_id="host"),
                _seat(1, LobbySeatKind.AI),
            ),
            LobbyReadinessReason.UNRESOLVED_AI_SEAT,
        ),
        (
            (_member("host"),),
            (_seat(0, LobbySeatKind.HUMAN, member_id="host"),),
            LobbyReadinessReason.COUNT_MISMATCH,
        ),
    ],
)
def test_readiness_reports_each_reason_token(
    members: tuple[LobbyReadinessMember, ...],
    seats: tuple[LobbyReadinessSeat, ...],
    reason: LobbyReadinessReason,
) -> None:
    decision = evaluate_lobby_readiness(
        members=members,
        seats=seats,
        expected_player_count=2,
        available_ai_build_count=0,
    )

    assert decision.allowed is False
    assert reason in decision.reasons


def test_readiness_treats_unknown_ai_resolution_as_injected_policy() -> None:
    members = (_member("host"),)
    seats = (
        _seat(0, "HUMAN", member_id="host"),
        _seat(1, "AI"),
        _seat(2, "AI"),
    )

    lock_stage = evaluate_lobby_readiness(
        members=members,
        seats=seats,
        expected_player_count=3,
        available_ai_build_count=None,
    )
    launch_stage = evaluate_lobby_readiness(
        members=members,
        seats=seats,
        expected_player_count=3,
        available_ai_build_count=1,
    )

    assert lock_stage.allowed is True
    assert launch_stage.allowed is False
    assert launch_stage.reasons == (LobbyReadinessReason.UNRESOLVED_AI_SEAT,)
