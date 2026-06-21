"""Tests for the agent response contract and parser."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from padrino.core.agents.contract import (
    REASON_INVALID_JSON,
    REASON_SCHEMA_VIOLATION,
    AgentResponse,
    ResponseError,
    parse_agent_response,
)
from padrino.core.engine.actions import Action
from padrino.core.enums import ActionType


def _valid_payload() -> dict[str, object]:
    return {
        "public_message": "I think P03 is suspicious.",
        "private_message": None,
        "action": {"type": "VOTE", "target": "P03"},
        "memory_update": "P03 acted defensively when pressed.",
        "rationale_summary": "Voting P03 to gather information.",
    }


def test_parse_valid_response() -> None:
    raw = json.dumps(_valid_payload())
    result = parse_agent_response(raw)
    assert isinstance(result, AgentResponse)
    assert result.public_message == "I think P03 is suspicious."
    assert result.private_message is None
    assert result.action == Action(type=ActionType.VOTE, target="P03")
    assert result.memory_update == "P03 acted defensively when pressed."
    assert result.rationale_summary == "Voting P03 to gather information."


def test_parse_noop_action_with_null_messages() -> None:
    payload = {
        "public_message": None,
        "private_message": None,
        "action": {"type": "NOOP", "target": None},
        "memory_update": "",
        "rationale_summary": None,
    }
    result = parse_agent_response(json.dumps(payload))
    assert isinstance(result, AgentResponse)
    assert result.action.type is ActionType.NOOP
    assert result.action.target is None


@pytest.mark.parametrize(
    "action_type",
    ["ROLEBLOCK", "FRAME", "TRACK", "WATCH", "CLEAN"],
)
def test_parse_new_night_action_types(action_type: str) -> None:
    payload = _valid_payload()
    payload["action"] = {"type": action_type, "target": "P03"}
    result = parse_agent_response(json.dumps(payload))
    assert isinstance(result, AgentResponse)
    assert result.action.type is ActionType(action_type)
    assert result.action.target == "P03"


def test_parse_invalid_json_returns_response_error() -> None:
    result = parse_agent_response("{not valid json")
    assert isinstance(result, ResponseError)
    assert result.reason == REASON_INVALID_JSON
    assert result.details is not None


def test_parse_empty_string_returns_invalid_json() -> None:
    result = parse_agent_response("")
    assert isinstance(result, ResponseError)
    assert result.reason == REASON_INVALID_JSON


def test_parse_non_object_top_level_is_schema_violation() -> None:
    result = parse_agent_response("[1, 2, 3]")
    assert isinstance(result, ResponseError)
    assert result.reason == REASON_SCHEMA_VIOLATION


def test_parse_string_top_level_is_schema_violation() -> None:
    result = parse_agent_response('"hello"')
    assert isinstance(result, ResponseError)
    assert result.reason == REASON_SCHEMA_VIOLATION


def test_parse_missing_public_message_is_schema_violation() -> None:
    payload = _valid_payload()
    del payload["public_message"]
    result = parse_agent_response(json.dumps(payload))
    assert isinstance(result, ResponseError)
    assert result.reason == REASON_SCHEMA_VIOLATION


def test_parse_missing_action_is_schema_violation() -> None:
    payload = _valid_payload()
    del payload["action"]
    result = parse_agent_response(json.dumps(payload))
    assert isinstance(result, ResponseError)
    assert result.reason == REASON_SCHEMA_VIOLATION


def test_parse_missing_memory_update_is_schema_violation() -> None:
    payload = _valid_payload()
    del payload["memory_update"]
    result = parse_agent_response(json.dumps(payload))
    assert isinstance(result, ResponseError)
    assert result.reason == REASON_SCHEMA_VIOLATION


def test_parse_wrong_type_for_memory_update_is_schema_violation() -> None:
    payload = _valid_payload()
    payload["memory_update"] = 42
    result = parse_agent_response(json.dumps(payload))
    assert isinstance(result, ResponseError)
    assert result.reason == REASON_SCHEMA_VIOLATION


def test_parse_wrong_type_for_public_message_is_schema_violation() -> None:
    payload = _valid_payload()
    payload["public_message"] = 123
    result = parse_agent_response(json.dumps(payload))
    assert isinstance(result, ResponseError)
    assert result.reason == REASON_SCHEMA_VIOLATION


def test_parse_invalid_action_type_is_schema_violation() -> None:
    payload = _valid_payload()
    payload["action"] = {"type": "BANANA", "target": "P03"}
    result = parse_agent_response(json.dumps(payload))
    assert isinstance(result, ResponseError)
    assert result.reason == REASON_SCHEMA_VIOLATION


def test_parse_action_missing_type_is_schema_violation() -> None:
    payload = _valid_payload()
    payload["action"] = {"target": "P03"}
    result = parse_agent_response(json.dumps(payload))
    assert isinstance(result, ResponseError)
    assert result.reason == REASON_SCHEMA_VIOLATION


def test_parse_extra_top_level_field_is_schema_violation() -> None:
    payload = _valid_payload()
    payload["sneaky_extra"] = "should not be allowed"
    result = parse_agent_response(json.dumps(payload))
    assert isinstance(result, ResponseError)
    assert result.reason == REASON_SCHEMA_VIOLATION


def test_parse_does_not_truncate_overlimit_message() -> None:
    huge_message = "X" * 100_000
    payload = _valid_payload()
    payload["public_message"] = huge_message
    payload["memory_update"] = huge_message
    result = parse_agent_response(json.dumps(payload))
    assert isinstance(result, AgentResponse)
    assert result.public_message == huge_message
    assert result.memory_update == huge_message


def test_agent_response_is_frozen() -> None:
    response = AgentResponse(
        public_message=None,
        private_message=None,
        action=Action(type=ActionType.NOOP, target=None),
        memory_update="",
        rationale_summary=None,
    )
    with pytest.raises(ValidationError):
        response.memory_update = "mutated"  # type: ignore[misc]


def test_response_error_is_frozen() -> None:
    err = ResponseError(reason=REASON_INVALID_JSON, details="bad")
    with pytest.raises(ValidationError):
        err.reason = REASON_SCHEMA_VIOLATION  # type: ignore[misc]


def test_response_error_details_optional() -> None:
    err = ResponseError(reason=REASON_INVALID_JSON)
    assert err.details is None


def test_parser_never_raises_on_arbitrary_garbage() -> None:
    samples = [
        "",
        "\x00\x01\x02",
        "not json",
        "{}",
        "null",
        "true",
        "42",
        '{"public_message": "x"}',
    ]
    for raw in samples:
        result = parse_agent_response(raw)
        assert isinstance(result, AgentResponse | ResponseError)


def test_parse_action_target_optional() -> None:
    payload = _valid_payload()
    payload["action"] = {"type": "ABSTAIN", "target": None}
    result = parse_agent_response(json.dumps(payload))
    assert isinstance(result, AgentResponse)
    assert result.action.target is None


def test_contract_module_has_no_forbidden_imports() -> None:
    """Pure-core firewall: no DB / LLM / runner / clock / random imports."""

    src = Path("src/padrino/core/agents/contract.py").read_text()
    tree = ast.parse(src)
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
        "time",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert alias.name not in forbidden, alias.name
                assert root not in forbidden, root
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            root = node.module.split(".")[0]
            assert node.module not in forbidden, node.module
            assert root not in forbidden, root
