"""Recorded-cassette contract tests for :class:`LiteLlmAdapter`.

These tests run only with the ``live_llm`` marker selected — the default
``pytest`` invocation deselects them (see ``tests/conftest.py``). They drive
:func:`litellm.acompletion` through ``vcrpy`` cassettes committed under
``tests/llm/cassettes/<provider>/`` and assert that our adapter parses each
provider's wire-format response into a valid :class:`AgentResponse`, and that
an intentionally malformed response round-trips cleanly through
:func:`coerce_response_failure`.

Re-recording
------------
Cassettes are committed under ``tests/llm/cassettes/<provider>/``. To
re-record against a real provider, export ``PADRINO_RECORD_LLM=1`` together
with the provider's API key env var, then run the contract tests with
``--live-llm``::

    export PADRINO_RECORD_LLM=1
    export OPENAI_API_KEY=sk-...
    uv run pytest tests/llm/test_litellm_contract.py --live-llm \\
        -m live_llm -k openai

The ``before_record_request`` / ``before_record_response`` hooks scrub
``authorization``, ``x-api-key``, and ``api_key`` JSON fields. CI never
records — the CI job runs with cassettes only and no network access.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import vcr
from vcr.request import Request as VcrRequest

from padrino.core.agents.coercion import coerce_response_failure
from padrino.core.agents.contract import (
    AgentResponse,
    ResponseError,
)
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import Faction, PhaseKind, Role
from padrino.core.observations import Observation, build_observation
from padrino.core.rulesets import mini7_v1
from padrino.llm.adapter import AdapterResult, AgentBuild, RoutingPolicy
from padrino.llm.litellm_adapter import LiteLlmAdapter
from padrino.llm.retry import DEFAULT_RETRY_ON, RetryPolicy

_CASSETTE_DIR: Path = Path(__file__).parent / "cassettes"
_CANONICAL_PHASE: Phase = Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)
_CONTRACT_RETRY_POLICY = RetryPolicy(
    max_attempts=1,
    base_delay_s=0.0,
    max_delay_s=0.0,
    retry_on=DEFAULT_RETRY_ON,
)

_SCRUB_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "x-api-key",
        "api-key",
        "cookie",
        "set-cookie",
        "openai-organization",
        "openai-project",
        "anthropic-organization-id",
    }
)

# Audit patterns: substrings that look like real provider secrets. The
# committed cassettes must not contain any of these — every credential is
# replaced with the literal "SCRUBBED" sentinel by the record-time hooks.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"tp-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-]{20,}", re.IGNORECASE),
)


@dataclass(frozen=True)
class ProviderCase:
    """One row of the contract-test parametrization grid.

    ``synthetic_canonical`` is True when the committed
    ``canonical_response.yaml`` was NOT recorded against a real provider — i.e.
    it is a hand-authored synthetic envelope that asserts the parser handles a
    shape we made up, not the shape the provider actually emits. Re-record
    with ``PADRINO_RECORD_LLM=1`` and the matching API key to flip this flag
    to False (US-072).
    """

    name: str
    model: str
    provider: str | None = None
    cassette_dir: str | None = None
    canonical_cassette: str = "canonical_response"
    malformed_cassette: str = "malformed_response"
    auth_secret_ref: str = "env:PADRINO_CONTRACT_AUTH_SECRET"
    api_base: str | None = None
    malformed_system_prompt: str | None = None
    synthetic_canonical: bool = False

    @property
    def provider_name(self) -> str:
        return self.provider if self.provider is not None else self.name

    @property
    def cassette_subdir(self) -> str:
        return self.cassette_dir if self.cassette_dir is not None else self.name


PROVIDER_CASES: tuple[ProviderCase, ...] = (
    # TODO(US-072): re-record once an OPENAI_API_KEY is provisioned for the
    # maintainer; current cassette is synthetic.
    ProviderCase("openai", "openai/gpt-4o-mini", synthetic_canonical=True),
    # TODO(US-072): re-record once an ANTHROPIC_API_KEY is provisioned for
    # the maintainer; current cassette is synthetic.
    ProviderCase("anthropic", "anthropic/claude-haiku-4-5", synthetic_canonical=True),
    ProviderCase("cerebras", "cerebras/zai-glm-4.7"),
    ProviderCase("deepinfra", "deepinfra/deepseek-ai/DeepSeek-V4-Flash"),
    ProviderCase(
        "xiaomi-mimo-v25",
        "openai/mimo-v2.5",
        provider="xiaomi",
        cassette_dir="xiaomi",
        canonical_cassette="mimo_v25_canonical_response",
        malformed_cassette="mimo_v25_malformed_response",
        auth_secret_ref="env:XIAOMI_API_KEY",
        api_base="https://token-plan-sgp.xiaomimimo.com/v1",
        malformed_system_prompt=(
            "Return exactly this text, with no JSON and no code fence: oops not json {"
        ),
    ),
    ProviderCase(
        "xiaomi-mimo-v25-pro",
        "openai/mimo-v2.5-pro",
        provider="xiaomi",
        cassette_dir="xiaomi",
        canonical_cassette="mimo_v25_pro_canonical_response",
        malformed_cassette="mimo_v25_pro_malformed_response",
        auth_secret_ref="env:XIAOMI_API_KEY",
        api_base="https://token-plan-sgp.xiaomimimo.com/v1",
        malformed_system_prompt=(
            "Return exactly this text, with no JSON and no code fence: oops not json {"
        ),
    ),
    # TODO(US-072): re-record once a local Ollama instance is reachable from
    # the maintainer's recording shell; current cassette is synthetic.
    ProviderCase("ollama", "ollama/llama3", synthetic_canonical=True),
)


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
    state = GameState(
        ruleset_id=mini7_v1.RULESET_ID,
        game_id="G-CONTRACT",
        game_seed="seed-contract",
        current_phase=_CANONICAL_PHASE,
        seats=_SEATS,
        day=_CANONICAL_PHASE.day,
    )
    return build_observation(state, _SEATS[0], EventLog(), mini7_v1)


def _scrub_request(request: VcrRequest) -> VcrRequest:
    """``before_record_request`` hook: drop auth headers and any JSON ``api_key``."""
    for header in list(request.headers):
        if header.lower() in _SCRUB_HEADER_NAMES:
            request.headers[header] = "SCRUBBED"
    body = request.body
    body_text: str | None
    if isinstance(body, (bytes, bytearray)):
        body_text = body.decode("utf-8", errors="ignore")
    elif isinstance(body, str):
        body_text = body
    else:
        body_text = None
    if body_text:
        try:
            payload: Any = json.loads(body_text)
        except (json.JSONDecodeError, ValueError):
            return request
        if isinstance(payload, dict) and "api_key" in payload:
            payload["api_key"] = "SCRUBBED"
            request.body = json.dumps(payload).encode("utf-8")
    return request


def _scrub_response(response: dict[str, Any]) -> dict[str, Any]:
    """``before_record_response`` hook: drop auth-related response headers."""
    headers = response.get("headers", {})
    if isinstance(headers, dict):
        for header in list(headers):
            if header.lower() in _SCRUB_HEADER_NAMES:
                headers[header] = ["SCRUBBED"]
    return response


def _recording_enabled() -> bool:
    return os.environ.get("PADRINO_RECORD_LLM") == "1"


def _build_vcr(*, record_mode: str | None = None) -> vcr.VCR:
    selected_record_mode = record_mode
    if selected_record_mode is None:
        selected_record_mode = "once" if _recording_enabled() else "none"
    return vcr.VCR(
        cassette_library_dir=str(_CASSETTE_DIR),
        record_mode=selected_record_mode,
        match_on=("method", "scheme", "host", "path"),
        decode_compressed_response=True,
        filter_headers=tuple(_SCRUB_HEADER_NAMES),
        before_record_request=_scrub_request,
        before_record_response=_scrub_response,
    )


def _build_adapter(case: ProviderCase) -> LiteLlmAdapter:
    # Replay against committed cassettes is instant, so the default 5 s
    # timeout is plenty. While recording (``PADRINO_RECORD_LLM=1``) a real
    # provider's cold-start latency can spike above that — DeepInfra's
    # DeepSeek-V4-Flash regularly takes 15+ s on first contact — so we
    # extend the timeout to 60 s during record runs. The env var is a
    # recording-only knob and is not consulted in CI.
    timeout_s = 60.0 if _recording_enabled() else 5.0
    return LiteLlmAdapter(
        routing_policy=RoutingPolicy(primary_model=case.model, fallback_model=None),
        agent_build=AgentBuild(
            provider=case.provider_name,
            model_id=case.model,
            prompt_version="contract_v1",
            inference_params={},
            adapter_version="litellm-cassette-1",
        ),
        timeout_s=timeout_s,
        auth_secret_ref=case.auth_secret_ref,
        api_base=case.api_base,
        retry_policy=_CONTRACT_RETRY_POLICY,
    )


@pytest.fixture(autouse=True)
def _stub_provider_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject placeholder credentials so litellm dispatches during cassette replay.

    When ``PADRINO_RECORD_LLM=1`` the developer's real credentials are
    expected to be present in the shell; we leave them untouched so the
    re-record run can authenticate against the real provider.
    """
    monkeypatch.setenv("PADRINO_CONTRACT_AUTH_SECRET", "cassette-replay-stub")
    if _recording_enabled():
        return
    monkeypatch.setenv("OPENAI_API_KEY", "sk-cassette-replay-stub")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-cassette-replay-stub")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cassette-replay-stub")
    monkeypatch.setenv("DEEPINFRA_API_KEY", "cassette-replay-stub")
    monkeypatch.setenv("XIAOMI_API_KEY", "tp-cassette-replay-stub")


def _cassette_or_skip(case: ProviderCase, name: str) -> Path:
    path = _CASSETTE_DIR / case.cassette_subdir / f"{name}.yaml"
    if not path.exists() and os.environ.get("PADRINO_RECORD_LLM") != "1":
        pytest.skip(
            f"missing cassette {path.relative_to(_CASSETTE_DIR.parent.parent)}; "
            "set PADRINO_RECORD_LLM=1 and the provider credential to record"
        )
    return path


async def _complete_with_cassette(
    adapter: LiteLlmAdapter,
    obs: Observation,
    cassette_path: Path,
) -> AdapterResult:
    """Complete once under VCR; replay immediately after record-time parse quirks."""
    my_vcr = _build_vcr()
    with my_vcr.use_cassette(str(cassette_path)):
        result = await adapter.complete(obs)
    if _recording_enabled() and result.status == "exhausted" and cassette_path.exists():
        replay_vcr = _build_vcr(record_mode="none")
        with replay_vcr.use_cassette(str(cassette_path)):
            result = await adapter.complete(obs)
    return result


@pytest.mark.live_llm
@pytest.mark.parametrize(
    "case",
    PROVIDER_CASES,
    ids=[c.name for c in PROVIDER_CASES],
)
async def test_canonical_response_parses(case: ProviderCase) -> None:
    """Each provider's canonical wire-format response must validate against AgentResponse."""
    if case.synthetic_canonical:
        pytest.skip(
            f"{case.name}: canonical_response.yaml is synthetic (US-072 — "
            "re-record with PADRINO_RECORD_LLM=1 once the provider key is "
            "provisioned). Skipping so the live_llm collection count reflects "
            "only provider-recorded contracts."
        )
    cassette_path = _cassette_or_skip(case, case.canonical_cassette)
    adapter = _build_adapter(case)
    obs = _canonical_observation()
    result = await _complete_with_cassette(adapter, obs, cassette_path)

    assert isinstance(result, AdapterResult)
    assert result.status == "ok", (
        f"{case.name}: expected status=ok, got {result.status!r} error={result.error!r}"
    )
    assert isinstance(result.parsed_response, AgentResponse), (
        f"{case.name}: expected AgentResponse, got "
        f"{type(result.parsed_response).__name__}: {result.parsed_response!r}"
    )
    assert result.raw_response, f"{case.name}: empty raw_response"


@pytest.mark.live_llm
@pytest.mark.parametrize(
    "case",
    PROVIDER_CASES,
    ids=[c.name for c in PROVIDER_CASES],
)
async def test_malformed_response_coerces(case: ProviderCase) -> None:
    """A malformed wire-format response must surface as ResponseError, coercible to a safe action."""
    cassette_path = _cassette_or_skip(case, case.malformed_cassette)
    adapter = _build_adapter(case)
    obs = _canonical_observation()
    if case.malformed_system_prompt is not None:
        adapter = LiteLlmAdapter(
            routing_policy=RoutingPolicy(primary_model=case.model, fallback_model=None),
            agent_build=AgentBuild(
                provider=case.provider_name,
                model_id=case.model,
                prompt_version="contract_v1",
                inference_params={},
                adapter_version="litellm-cassette-1",
            ),
            timeout_s=60.0 if _recording_enabled() else 5.0,
            auth_secret_ref=case.auth_secret_ref,
            api_base=case.api_base,
            system_prompt=case.malformed_system_prompt,
            retry_policy=_CONTRACT_RETRY_POLICY,
        )
    result = await _complete_with_cassette(adapter, obs, cassette_path)

    assert isinstance(result, AdapterResult)
    assert isinstance(result.parsed_response, ResponseError), (
        f"{case.name}: expected ResponseError, got "
        f"{type(result.parsed_response).__name__}: {result.parsed_response!r}"
    )
    safe = coerce_response_failure(_CANONICAL_PHASE, result.parsed_response.reason)
    assert isinstance(safe, AgentResponse)
    # Coercion in DAY_VOTE collapses to ABSTAIN with no side payloads.
    assert safe.action.target is None
    assert safe.public_message is None
    assert safe.private_message is None
    assert safe.memory_update == ""


def test_cassettes_have_no_secret_shaped_substrings() -> None:
    """Audit every committed cassette for credential-shaped substrings.

    Belt-and-suspenders to the record-time scrubbing hooks: if a real key
    ever sneaks through, this test fails before the cassette is committed.
    """
    cassette_files = sorted(_CASSETTE_DIR.rglob("*.yaml"))
    assert cassette_files, "no cassettes found — provider contract suite needs at least one"
    offenders = _scan_for_secret_patterns(cassette_files)
    assert not offenders, "secret-shaped substring(s) found in cassettes:\n  " + "\n  ".join(
        offenders
    )


def _scan_for_secret_patterns(paths: list[Path]) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        try:
            label = str(path.relative_to(_CASSETTE_DIR.parent.parent))
        except ValueError:
            label = str(path)
        for pattern in _SECRET_PATTERNS:
            for match in pattern.findall(text):
                offenders.append(f"{label}: {match!r}")
    return offenders


def test_audit_catches_deliberately_leaky_probe(tmp_path: Path) -> None:
    """The cassette audit must flag a deliberately-leaky probe response.

    US-072 acceptance: scrubbing hooks are belt-and-suspenders only; the
    post-write audit must independently detect a credential-shaped substring
    in a written cassette. This test plants three probe patterns (one per
    member of ``_SECRET_PATTERNS``) in a tmp cassette and asserts the audit
    flags all three — never trust the regex set silently going stale.
    """
    probes: tuple[tuple[str, str], ...] = (
        ("openai_probe", "sk-1234567890ABCDEFGHIJ0987654321ZYXWvutsrq"),
        ("anthropic_probe", "sk-ant-api03-deadBeef1234567890_-ZyXwVuTsRq"),
        ("xiaomi_probe", "tp-1234567890ABCDEFGHIJ0987654321ZYXWvutsrq"),
        ("bearer_probe", "Bearer abcdef0123456789ABCDEF0123456789xyzwvu"),
    )
    leaky_path = tmp_path / "leaky_probe.yaml"
    leaky_path.write_text(
        "interactions:\n"
        "- response:\n"
        "    body:\n      string: |\n"
        + "".join(f"        {name}={value}\n" for name, value in probes)
        + "version: 1\n",
        encoding="utf-8",
    )

    offenders = _scan_for_secret_patterns([leaky_path])

    leaked_substrings = {offender.split(": ", 1)[1].strip("'") for offender in offenders}
    for _name, value in probes:
        assert value in leaked_substrings, (
            f"audit failed to flag probe {value!r} — _SECRET_PATTERNS has drifted "
            f"out of sync with the patterns it claims to detect. Offenders: {offenders!r}"
        )


def test_every_provider_case_has_cassettes() -> None:
    """Each parametrized provider must ship both a canonical and a malformed cassette."""
    missing: list[str] = []
    for case in PROVIDER_CASES:
        for name in (case.canonical_cassette, case.malformed_cassette):
            path = _CASSETTE_DIR / case.cassette_subdir / f"{name}.yaml"
            if not path.exists():
                missing.append(str(path.relative_to(_CASSETTE_DIR.parent.parent)))
    assert not missing, "missing cassettes:\n  " + "\n  ".join(missing)
