"""Tests for schema-failure coercion to engine-safe AgentResponse."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from padrino.core.agents.coercion import (
    coerce_response_failure,
    coerce_to_safe_action,
)
from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.state import Phase
from padrino.core.enums import ActionType, PhaseKind

_ERROR_REASONS = [
    "INVALID_JSON",
    "SCHEMA_VIOLATION",
    "TIMEOUT",
    "ADAPTER_ERROR",
    "",
    "anything-else",
]


@pytest.mark.parametrize("reason", _ERROR_REASONS)
def test_day_vote_phase_coerces_to_abstain(reason: str) -> None:
    phase = Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)
    action = coerce_to_safe_action(phase, reason)
    assert action == Action(type=ActionType.ABSTAIN, target=None)


@pytest.mark.parametrize("reason", _ERROR_REASONS)
def test_night_actions_phase_coerces_to_noop(reason: str) -> None:
    phase = Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0)
    action = coerce_to_safe_action(phase, reason)
    assert action == Action(type=ActionType.NOOP, target=None)


@pytest.mark.parametrize("reason", _ERROR_REASONS)
def test_day_discussion_phase_coerces_to_noop(reason: str) -> None:
    phase = Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1)
    action = coerce_to_safe_action(phase, reason)
    assert action == Action(type=ActionType.NOOP, target=None)


@pytest.mark.parametrize(
    "kind",
    [
        PhaseKind.SETUP,
        PhaseKind.NIGHT_0_MAFIA_INTRO,
        PhaseKind.DAY_DISCUSSION,
        PhaseKind.NIGHT_MAFIA_DISCUSSION,
        PhaseKind.NIGHT_ACTIONS,
        PhaseKind.TERMINAL,
    ],
)
def test_non_vote_phases_all_coerce_to_noop(kind: PhaseKind) -> None:
    phase = Phase(kind=kind, day=1, round=0)
    action = coerce_to_safe_action(phase, "SCHEMA_VIOLATION")
    assert action.type is ActionType.NOOP
    assert action.target is None


def test_only_day_vote_yields_abstain() -> None:
    abstain_phase = Phase(kind=PhaseKind.DAY_VOTE, day=2, round=0)
    assert coerce_to_safe_action(abstain_phase, "x").type is ActionType.ABSTAIN
    other = Phase(kind=PhaseKind.NIGHT_ACTIONS, day=2, round=0)
    assert coerce_to_safe_action(other, "x").type is ActionType.NOOP


def test_coerce_response_failure_vote_phase() -> None:
    phase = Phase(kind=PhaseKind.DAY_VOTE, day=3, round=0)
    response = coerce_response_failure(phase, "INVALID_JSON")
    assert isinstance(response, AgentResponse)
    assert response.public_message is None
    assert response.private_message is None
    assert response.action == Action(type=ActionType.ABSTAIN, target=None)
    assert response.memory_update == ""
    assert response.rationale_summary is None


def test_coerce_response_failure_night_actions() -> None:
    phase = Phase(kind=PhaseKind.NIGHT_ACTIONS, day=2, round=0)
    response = coerce_response_failure(phase, "TIMEOUT")
    assert isinstance(response, AgentResponse)
    assert response.public_message is None
    assert response.private_message is None
    assert response.action == Action(type=ActionType.NOOP, target=None)
    assert response.memory_update == ""
    assert response.rationale_summary is None


def test_coerce_response_failure_discussion() -> None:
    phase = Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=2)
    response = coerce_response_failure(phase, "SCHEMA_VIOLATION")
    assert response.action == Action(type=ActionType.NOOP, target=None)
    assert response.public_message is None
    assert response.memory_update == ""


@pytest.mark.parametrize("reason", _ERROR_REASONS)
def test_response_failure_is_invariant_to_error_reason(reason: str) -> None:
    phase = Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0)
    a = coerce_response_failure(phase, reason)
    b = coerce_response_failure(phase, "OTHER_REASON")
    assert a == b


def test_response_failure_is_frozen() -> None:
    from pydantic import ValidationError

    phase = Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)
    response = coerce_response_failure(phase, "x")
    with pytest.raises(ValidationError):
        response.memory_update = "mutated"  # type: ignore[misc]


def test_safe_action_is_frozen() -> None:
    from pydantic import ValidationError

    phase = Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)
    action = coerce_to_safe_action(phase, "x")
    with pytest.raises(ValidationError):
        action.target = "P03"  # type: ignore[misc]


def test_no_forbidden_imports_in_coercion_module() -> None:
    path = (
        Path(__file__).resolve().parents[3] / "src" / "padrino" / "core" / "agents" / "coercion.py"
    )
    source = path.read_text()
    tree = ast.parse(source)
    forbidden = {
        "padrino.db",
        "padrino.llm",
        "padrino.api",
        "padrino.runner",
        "sqlalchemy",
        "litellm",
        "httpx",
        "random",
        "secrets",
        "datetime",
        "time",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in forbidden, node.module
