"""Block-before-release human-chat moderation (US-140).

The real-time gate runs INSIDE the buffer hold window before any other seat sees
the message:

* a deterministic first-pass (pure single-message verdict) is the instant
  backstop — a hard hit is an immediate BLOCK and the guard is never called;
* the pure sanitizer + a pure deterministic span-mask produce the SOFT_MASK
  release text;
* an async guard model runs under a hard latency budget; on timeout/error the
  gate falls back to the deterministic verdict for THAT message — the game NEVER
  halts;
* a BLOCK is never released and never chained; the released/masked text persists
  to the out-of-band sidecar (US-123), never reaching another seat inline.

A recording stub proves the gate actually SAW the message (guards the Wave-7
vacuous-gate bug class).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from http.cookies import SimpleCookie
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.api.human_chat import submit_chat
from padrino.api.human_chat_moderation import (
    ChatModerationVerdict,
    ChatVerdict,
    LiteLlmMessageGuardAdapter,
    MessageGuardAdapter,
    RealtimeModerationHook,
    StubPassModerationHook,
    build_message_guard_from_settings,
)
from padrino.api.rate_limit_store import InMemoryRateLimitStore
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import SeatKind
from padrino.core.rulesets import mini7_v1
from padrino.db.models import (
    Game,
    GameSeat,
    HumanChatMessage,
    HumanChatSubmission,
    Principal,
)
from padrino.db.repositories import events as events_repo
from padrino.public.moderation import (
    deterministic_first_pass_message,
    deterministic_span_mask,
)

_GAME_SEED = "mod-seed"
_PHASE = "DAY_1_DISCUSSION_ROUND_1"
_HUMAN_SEAT = "P03"


# ---------------------------------------------------------------------------
# Pure single-message verdict + span-mask
# ---------------------------------------------------------------------------


def test_first_pass_message_blocks_hard_hit() -> None:
    assert deterministic_first_pass_message("a perfectly fine message") is True
    assert deterministic_first_pass_message("this is toxic_word here") is False


def test_span_mask_is_pure_and_deterministic() -> None:
    masked_a, did_a = deterministic_span_mask("oh mask_word what")
    masked_b, did_b = deterministic_span_mask("oh mask_word what")
    assert did_a is True and did_b is True
    assert masked_a == masked_b == "oh ********* what"
    clean, did_clean = deterministic_span_mask("nothing to see")
    assert did_clean is False
    assert clean == "nothing to see"


# ---------------------------------------------------------------------------
# RealtimeModerationHook verdicts + hardened fail path
# ---------------------------------------------------------------------------


class _RecordingGuard:
    """Records every message it saw and answers with a fixed verdict."""

    def __init__(self, *, safe: bool) -> None:
        self.safe = safe
        self.seen: list[str] = []

    async def check_message(self, text: str) -> bool:
        self.seen.append(text)
        return self.safe


class _TimeoutGuard:
    """Never returns within the budget — proves the hardened fail path."""

    def __init__(self) -> None:
        self.calls = 0

    async def check_message(self, text: str) -> bool:
        self.calls += 1
        await asyncio.sleep(10.0)
        return True  # pragma: no cover - the wait_for times out first


class _ErrorGuard:
    async def check_message(self, text: str) -> bool:
        raise RuntimeError("guard model unavailable")


@pytest.mark.asyncio
async def test_hard_hit_blocks_without_calling_guard() -> None:
    guard = _RecordingGuard(safe=True)
    hook = RealtimeModerationHook(guard=guard)
    verdict = await hook.review(public_player_id=_HUMAN_SEAT, channel="PUBLIC", text="kys loser")
    assert verdict == ChatModerationVerdict(verdict=ChatVerdict.BLOCK, cleaned_text=None)
    # A hard hit never reaches the guard model.
    assert guard.seen == []


@pytest.mark.asyncio
async def test_guard_block_is_never_released() -> None:
    guard = _RecordingGuard(safe=False)
    hook = RealtimeModerationHook(guard=guard)
    verdict = await hook.review(
        public_player_id=_HUMAN_SEAT, channel="PUBLIC", text="a borderline message"
    )
    assert verdict.verdict is ChatVerdict.BLOCK
    assert verdict.cleaned_text is None
    # The recording stub proves the gate actually saw the message.
    assert guard.seen == ["a borderline message"]


@pytest.mark.asyncio
async def test_soft_mask_releases_masked_text() -> None:
    guard = _RecordingGuard(safe=True)
    hook = RealtimeModerationHook(guard=guard)
    verdict = await hook.review(
        public_player_id=_HUMAN_SEAT, channel="PUBLIC", text="oh mask_word that play"
    )
    assert verdict.verdict is ChatVerdict.SOFT_MASK
    assert verdict.cleaned_text == "oh ********* that play"
    # The guard is shown the already-masked candidate, never the raw span.
    assert guard.seen == ["oh ********* that play"]


@pytest.mark.asyncio
async def test_clean_message_allowed_after_guard() -> None:
    guard = _RecordingGuard(safe=True)
    hook = RealtimeModerationHook(guard=guard)
    verdict = await hook.review(
        public_player_id=_HUMAN_SEAT, channel="PUBLIC", text="I vote for P04"
    )
    assert verdict.verdict is ChatVerdict.ALLOW
    assert verdict.cleaned_text == "I vote for P04"
    assert guard.seen == ["I vote for P04"]


@pytest.mark.asyncio
async def test_guard_timeout_falls_back_to_deterministic() -> None:
    guard = _TimeoutGuard()
    hook = RealtimeModerationHook(guard=guard, timeout_s=0.01)
    verdict = await hook.review(
        public_player_id=_HUMAN_SEAT, channel="PUBLIC", text="a fine message"
    )
    # Hardened fail path: the deterministic verdict stands; the game proceeds.
    assert verdict.verdict is ChatVerdict.ALLOW
    assert verdict.cleaned_text == "a fine message"
    assert guard.calls == 1


@pytest.mark.asyncio
async def test_guard_error_falls_back_to_deterministic() -> None:
    hook = RealtimeModerationHook(guard=_ErrorGuard())
    verdict = await hook.review(
        public_player_id=_HUMAN_SEAT, channel="PUBLIC", text="another fine message"
    )
    assert verdict.verdict is ChatVerdict.ALLOW
    assert verdict.cleaned_text == "another fine message"


@pytest.mark.asyncio
async def test_no_guard_uses_deterministic_only() -> None:
    hook = RealtimeModerationHook(guard=None)
    block = await hook.review(public_player_id=_HUMAN_SEAT, channel="PUBLIC", text="toxic_word")
    assert block.verdict is ChatVerdict.BLOCK
    soft = await hook.review(public_player_id=_HUMAN_SEAT, channel="PUBLIC", text="mask_word ok")
    assert soft.verdict is ChatVerdict.SOFT_MASK


# ---------------------------------------------------------------------------
# Concrete LiteLLM-backed guard adapter + production wiring (US-140)
# ---------------------------------------------------------------------------


def test_litellm_message_guard_satisfies_protocol() -> None:
    guard = LiteLlmMessageGuardAdapter(model="deepinfra/meta-llama/Llama-Guard-3-8B")
    assert isinstance(guard, MessageGuardAdapter)


def test_build_message_guard_returns_none_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from padrino.settings import Settings

    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    settings = Settings(deepinfra_api_key=None)
    assert build_message_guard_from_settings(settings) is None


def test_build_message_guard_constructs_adapter_from_settings() -> None:
    from padrino.settings import Settings

    settings = Settings(
        deepinfra_api_key="test-key",
        padrino_guard_model="deepinfra/meta-llama/Llama-Guard-3-8B",
        padrino_human_chat_guard_timeout_seconds=1.5,
    )
    guard = build_message_guard_from_settings(settings)
    assert isinstance(guard, LiteLlmMessageGuardAdapter)


@pytest.mark.asyncio
async def test_litellm_message_guard_parses_safe_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The concrete adapter maps a Llama-Guard ``safe``/``unsafe`` token to bool.

    Stubs ``litellm.acompletion`` so the default suite exercises the real
    adapter's response parsing without a network call.
    """
    import litellm

    class _Msg:
        def __init__(self, content: str) -> None:
            self.content = content

    class _Choice:
        def __init__(self, content: str) -> None:
            self.message = _Msg(content)

    class _Resp:
        def __init__(self, content: str) -> None:
            self.choices = [_Choice(content)]

    captured: dict[str, Any] = {}

    async def _fake_safe(**kwargs: Any) -> _Resp:
        captured.update(kwargs)
        return _Resp("safe")

    async def _fake_unsafe(**kwargs: Any) -> _Resp:
        return _Resp("unsafe\nS1")

    guard = LiteLlmMessageGuardAdapter(
        model="deepinfra/meta-llama/Llama-Guard-3-8B", api_key="k", timeout_s=1.0
    )

    monkeypatch.setattr(litellm, "acompletion", _fake_safe)
    assert await guard.check_message("hello town") is True
    assert captured["model"] == "deepinfra/meta-llama/Llama-Guard-3-8B"
    assert captured["api_key"] == "k"

    monkeypatch.setattr(litellm, "acompletion", _fake_unsafe)
    assert await guard.check_message("a bad message") is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_litellm_message_guard_real_call() -> None:
    """Real Llama-Guard call (skipped by default; requires DEEPINFRA_API_KEY)."""
    import os

    if not os.environ.get("DEEPINFRA_API_KEY"):
        pytest.skip("DEEPINFRA_API_KEY not set")

    from padrino.settings import Settings

    settings = Settings()
    guard = build_message_guard_from_settings(settings)
    assert guard is not None
    # A benign message must be judged safe by the real guard model.
    assert await guard.check_message("Good game everyone, I vote for P04.") is True


# ---------------------------------------------------------------------------
# Channel integration: block never reaches the sidecar, rate limits
# ---------------------------------------------------------------------------


def _discussion_phase_bodies(human_seat: str) -> list[dict[str, Any]]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    assignments = [
        {
            "public_player_id": s.public_player_id,
            "seat_index": s.seat_index,
            "role": s.role.value,
            "faction": s.faction.value,
            "seat_kind": (
                SeatKind.HUMAN.value if s.public_player_id == human_seat else SeatKind.AI.value
            ),
        }
        for s in seats
    ]
    return [
        {
            "event_type": "GameCreated",
            "sequence": 0,
            "phase": "SETUP",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {
                "ruleset_id": mini7_v1.RULESET_ID,
                "game_id": "g",
                "game_seed": _GAME_SEED,
                "player_count": mini7_v1.PLAYER_COUNT,
            },
        },
        {
            "event_type": "RolesAssigned",
            "sequence": 1,
            "phase": "SETUP",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {"assignments": assignments},
        },
        {
            "event_type": "PhaseStarted",
            "sequence": 2,
            "phase": _PHASE,
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {"phase_kind": "DAY_DISCUSSION", "day": 1, "round": 1},
        },
    ]


async def _seed_human_game(
    session: AsyncSession, *, principal_id: uuid.UUID | None, human_seat: str = _HUMAN_SEAT
) -> uuid.UUID:
    game = Game(
        gauntlet_id=None,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=_GAME_SEED,
        status="RUNNING",
    )
    session.add(game)
    await session.flush()

    log = EventLog()
    for body in _discussion_phase_bodies(human_seat):
        body = {**body, "payload": {**body["payload"]}}
        stored = log.append(body)
        await events_repo.append_event(
            session,
            game_id=game.id,
            sequence=stored.sequence,
            event_type=body["event_type"],
            phase=body["phase"],
            visibility=body["visibility"],
            actor_player_id=body["actor_player_id"],
            payload=body["payload"],
            prev_event_hash=stored.prev_event_hash,
            event_hash=stored.event_hash,
        )

    for s in assign_roles(_GAME_SEED, mini7_v1):
        is_human = s.public_player_id == human_seat
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id=s.public_player_id,
                seat_index=s.seat_index,
                agent_build_id=None,
                seat_kind=SeatKind.HUMAN.value if is_human else SeatKind.AI.value,
                role=s.role.value,
                faction=s.faction.value,
                alive=True,
                occupant_principal_id=principal_id if is_human else None,
            )
        )
    await session.flush()
    return game.id


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _guest_token(resp_headers: object) -> str:
    jar = SimpleCookie()
    for raw in resp_headers.get_list("set-cookie"):  # type: ignore[attr-defined]
        if raw.startswith(f"{HUMAN_SESSION_COOKIE}="):
            jar.load(raw)
    return jar[HUMAN_SESSION_COOKIE].value


async def _consenting_guest(client: AsyncClient) -> str:
    resp = await client.post("/human/guest")
    assert resp.status_code == 201
    token = _guest_token(resp.headers)
    consent = await client.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: token})
    assert consent.status_code == 201
    return token


async def _principal_id(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as session:
        principal = (await session.execute(select(Principal))).scalars().one()
    return principal.id


@pytest.mark.asyncio
async def test_blocked_message_never_released_to_sidecar(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _consenting_guest(client)
    pid = await _principal_id(session_factory)
    async with session_factory() as session, session.begin():
        game_id = await _seed_human_game(session, principal_id=pid)

    body = {"channel": "PUBLIC", "text": "toxic_word in here", "idempotency_key": "b1"}
    resp = await client.post(
        f"/human/games/{game_id}/chat", json=body, cookies={HUMAN_SESSION_COOKIE: token}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "BLOCKED"

    async with session_factory() as session:
        holds = (await session.execute(select(HumanChatSubmission))).scalars().all()
        sidecar = (await session.execute(select(HumanChatMessage))).scalars().all()
    assert len(holds) == 1
    assert holds[0].status == "BLOCKED"
    # A BLOCK is never released and never reaches the sidecar — no other seat
    # can ever see the raw text.
    assert sidecar == []


@pytest.mark.asyncio
async def test_per_principal_rate_limit_429(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pid = uuid.uuid4()
    async with session_factory() as session, session.begin():
        principal = Principal(id=pid, kind="guest")
        session.add(principal)
        game_id = await _seed_human_game(session, principal_id=pid)

    from datetime import UTC, datetime

    store = InMemoryRateLimitStore()
    from fastapi import HTTPException

    async with session_factory() as session, session.begin():
        for i in range(2):
            await submit_chat(
                session,
                game_id=game_id,
                principal_id=pid,
                channel="PUBLIC",
                text=f"clean message {i}",
                idempotency_key=f"k{i}",
                now=datetime.now(UTC),
                moderation=StubPassModerationHook(),
                rate_limit=store,
                per_principal_limit=2,
                per_game_phase_limit=100,
            )
        with pytest.raises(HTTPException) as excinfo:
            await submit_chat(
                session,
                game_id=game_id,
                principal_id=pid,
                channel="PUBLIC",
                text="over the cap",
                idempotency_key="k2",
                now=datetime.now(UTC),
                moderation=StubPassModerationHook(),
                rate_limit=store,
                per_principal_limit=2,
                per_game_phase_limit=100,
            )
    assert excinfo.value.status_code == 429
    assert excinfo.value.detail == "chat_rate_limited"


@pytest.mark.asyncio
async def test_idempotent_retry_does_not_consume_rate_slot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pid = uuid.uuid4()
    async with session_factory() as session, session.begin():
        session.add(Principal(id=pid, kind="guest"))
        game_id = await _seed_human_game(session, principal_id=pid)

    from datetime import UTC, datetime

    store = InMemoryRateLimitStore()
    async with session_factory() as session, session.begin():
        first = await submit_chat(
            session,
            game_id=game_id,
            principal_id=pid,
            channel="PUBLIC",
            text="hello town",
            idempotency_key="same",
            now=datetime.now(UTC),
            moderation=StubPassModerationHook(),
            rate_limit=store,
            per_principal_limit=1,
            per_game_phase_limit=100,
        )
        assert first.idempotent_replay is False
        # A retry under a cap of 1 must NOT 429 (it consumes no slot).
        retry = await submit_chat(
            session,
            game_id=game_id,
            principal_id=pid,
            channel="PUBLIC",
            text="hello town",
            idempotency_key="same",
            now=datetime.now(UTC),
            moderation=StubPassModerationHook(),
            rate_limit=store,
            per_principal_limit=1,
            per_game_phase_limit=100,
        )
    assert retry.idempotent_replay is True
