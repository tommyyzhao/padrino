"""Tolerant extraction of agent JSON from real model output shapes."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from padrino.core.agents.contract import (
    REASON_INVALID_JSON,
    AgentResponse,
    ResponseError,
    extract_json_object,
    parse_agent_response,
)

OBJ = {
    "public_message": 'P03 {looks} "off" \\ today',
    "private_message": None,
    "action": {"type": "VOTE", "target": "P03"},
    "memory_update": 'braces } { and quotes " inside strings',
    "rationale_summary": None,
}
OBJ_JSON = json.dumps(OBJ)
SECOND_JSON = json.dumps({"action": {"type": "NOOP", "target": None}})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(OBJ_JSON, OBJ, id="plain-json"),
        pytest.param(f"```json\n{OBJ_JSON}\n```", OBJ, id="fenced-with-newline"),
        pytest.param(f"```json{OBJ_JSON}```", OBJ, id="fenced-no-newline"),
        pytest.param(f"<think>weighing P03</think>\n{OBJ_JSON}", OBJ, id="think-bare-json"),
        pytest.param(
            f"<think>weighing P03</think>\n```json\n{OBJ_JSON}\n```",
            OBJ,
            id="think-fenced-json",
        ),
        pytest.param(f"<think>still reasoning {OBJ_JSON}", OBJ, id="unclosed-think-json-inside"),
        pytest.param(f"<think>truncated reasoning\n{OBJ_JSON}", OBJ, id="unclosed-think"),
        pytest.param(
            "<thinking>{ maybe } } {{ x</thinking>" + OBJ_JSON,
            OBJ,
            id="think-with-braces",
        ),
        pytest.param(f"<THINK>{{ upper }}</THINK>\n{OBJ_JSON}", OBJ, id="think-case-insensitive"),
        pytest.param(f"Here is my move: {OBJ_JSON} Good luck.", OBJ, id="prose-around"),
        pytest.param(f"Sure.\n{OBJ_JSON}\n", OBJ, id="prose-before-trailing-newline"),
        pytest.param(f"{OBJ_JSON} {SECOND_JSON}", OBJ, id="two-objects-first-wins"),
        pytest.param("no json here at all", None, id="no-json"),
        pytest.param("", None, id="empty"),
        pytest.param("  \n\t  ", None, id="whitespace-only"),
        pytest.param('{"a": 1,}', None, id="invalid-object-only"),
        pytest.param('{"a": "unterminated', None, id="unclosed-brace-string"),
    ],
)
def test_extract_json_object_corpus(raw: str, expected: dict[str, object] | None) -> None:
    result = extract_json_object(raw)
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert json.loads(result) == expected


def test_extract_json_object_returns_exact_span() -> None:
    assert extract_json_object(f"x {SECOND_JSON} y {OBJ_JSON}") == SECOND_JSON


def test_extract_json_object_skips_invalid_balanced_span() -> None:
    assert extract_json_object('note {see below} then {"a": 1}') == '{"a": 1}'


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not json",
        "```json\n```",
        '{"action": {"type": "VOTE"',
        "<think>no end and no object",
    ],
)
def test_parse_agent_response_no_object_is_invalid_json(raw: str) -> None:
    result = parse_agent_response(raw)
    assert isinstance(result, ResponseError)
    assert result.reason == REASON_INVALID_JSON


def test_parse_agent_response_recovers_think_fenced_payload() -> None:
    payload = {
        "public_message": "hi",
        "private_message": None,
        "action": {"type": "NOOP", "target": None},
        "memory_update": "",
        "rationale_summary": None,
    }
    raw = f"<think>{{x}}</think>\n```json\n{json.dumps(payload)}\n```"
    result = parse_agent_response(raw)
    assert isinstance(result, AgentResponse)
    assert result.public_message == "hi"


def test_parse_agent_response_schema_still_enforced_after_extraction() -> None:
    raw = 'prose {"public_message": "x"} prose'
    result = parse_agent_response(raw)
    assert isinstance(result, ResponseError)
    assert result.reason == "SCHEMA_VIOLATION"


_PROSE = st.text(alphabet=st.characters(blacklist_characters="{}<>"), max_size=80)


@given(
    before=_PROSE,
    after=_PROSE,
    public=st.text(max_size=40),
    memory=st.text(max_size=40),
)
def test_canonical_response_recovered_from_brace_free_prose(
    before: str, after: str, public: str, memory: str
) -> None:
    payload = {
        "public_message": public,
        "private_message": None,
        "action": {"type": "VOTE", "target": "P03"},
        "memory_update": memory,
        "rationale_summary": None,
    }
    canonical = json.dumps(payload)
    assert extract_json_object(before + canonical + after) == canonical


def test_stray_open_brace_before_object_is_skipped() -> None:
    assert extract_json_object(f"use {{ to start: {OBJ_JSON}") == OBJ_JSON
