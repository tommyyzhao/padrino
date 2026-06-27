"""US-299: identity-leak regression guards for observation and reveal routes.

Live observation and pre-reveal surfaces must stay identity-blind even after
ranked handles, verified emails, eligibility facts, and ranked bans exist in the
database. Terminal reveal may disclose AI model identity, but never human PII or
ranked enforcement state.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_SPECTATOR, RateLimiter, generate_raw_key
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import IdentityMode, SeatKind
from padrino.core.ranked_ban import RankedBanKind
from padrino.core.rulesets import mini7_v1
from padrino.db.models import (
    Game,
    GameSeat,
    OAuthIdentity,
    Principal,
    PromptVersion,
    RankedBan,
)
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import human_game_runtime as runtime_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import providers as providers_repo
from padrino.db.repositories.ranked_bans import hash_email_for_ranked_ban
from padrino.public.broadcast_index import BroadcastState
from padrino.settings import get_settings

pytestmark = pytest.mark.asyncio

_GAME_SEED = "identity-firewall-seed"
_CALLER_SEAT = "P03"
_OTHER_HUMAN_SEAT = "P04"
_PHASE = "DAY_1_VOTE"
_NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=UTC)
_DEADLINE = datetime(2026, 6, 27, 12, 5, 0, tzinfo=UTC)
_AI_PROVIDER = "us299-ai-provider"
_AI_MODEL = "us299-ai-model"
_AI_DISPLAY_NAME = "US299 AI Build Display"


@dataclass(frozen=True, slots=True)
class SensitiveHuman:
    """Human identity/enforcement facts that must not leak from game surfaces."""

    principal_id: uuid.UUID
    display_name: str
    ranked_handle: str
    email: str
    email_hash: str
    ban_reason: str
    ban_created_by: str


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _guest_token(resp_headers: object) -> str:
    jar = SimpleCookie()
    for raw in resp_headers.get_list("set-cookie"):  # type: ignore[attr-defined]
        if raw.startswith(f"{HUMAN_SESSION_COOKIE}="):
            jar.load(raw)
    return jar[HUMAN_SESSION_COOKIE].value


async def _guest(client: AsyncClient) -> tuple[str, uuid.UUID]:
    resp = await client.post("/human/guest")
    assert resp.status_code == 201
    return _guest_token(resp.headers), uuid.UUID(resp.json()["principal_id"])


async def _seed_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    label: str,
) -> str:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(session, raw_key=raw, scopes=[SCOPE_SPECTATOR], label=label)
    return raw


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def _make_agent_build(session: AsyncSession) -> uuid.UUID:
    provider = await providers_repo.create(
        session,
        name=_AI_PROVIDER,
        auth_secret_ref="env:MOCK_KEY",
    )
    mc = await model_configs_repo.create(
        session,
        provider_id=provider.id,
        model_name=_AI_MODEL,
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=512,
        supports_structured_outputs=False,
    )
    pv = PromptVersion(
        ruleset_id=mini7_v1.RULESET_ID,
        version="v1",
        system_prompt="system",
        developer_prompt="developer",
        response_schema={},
        prompt_hash=str(uuid.uuid4()),
    )
    session.add(pv)
    await session.flush()
    build = await agent_builds_repo.create(
        session,
        display_name=_AI_DISPLAY_NAME,
        model_config_id=mc.id,
        prompt_version_id=pv.id,
        adapter_version="1.0",
        inference_params={},
        active=True,
    )
    return build.id


async def _seed_sensitive_human(
    session: AsyncSession,
    *,
    label: str,
    principal_id: uuid.UUID | None = None,
) -> SensitiveHuman:
    normalized_label = label.lower().replace("_", "-")
    display_name = f"US299 {label} Display"
    ranked_handle = f"US299-{label}-Handle"
    email = f"us299-{normalized_label}@example.test"
    email_hash = hash_email_for_ranked_ban(email)
    if email_hash is None:  # pragma: no cover - constant non-empty email.
        raise AssertionError("email hash should be present")
    ban_reason = f"US299 {label} ranked ban reason"
    ban_created_by = f"us299-{normalized_label}-admin"

    principal: Principal
    if principal_id is None:
        principal = Principal(
            kind="account",
            display_name=display_name,
            ranked_handle=ranked_handle,
            ranked_handle_normalized=ranked_handle.lower(),
        )
        session.add(principal)
        await session.flush()
    else:
        existing = await session.get(Principal, principal_id)
        if existing is None:  # pragma: no cover - test setup sanity.
            raise AssertionError("principal should exist")
        principal = existing
        principal.display_name = display_name
        principal.ranked_handle = ranked_handle
        principal.ranked_handle_normalized = ranked_handle.lower()
    session.add(
        OAuthIdentity(
            provider="test-oauth",
            subject=f"subject-{normalized_label}",
            email=email,
            email_verified=True,
            principal_id=principal.id,
        )
    )
    session.add(
        RankedBan(
            principal_id=principal.id,
            email_hash=email_hash,
            kind=RankedBanKind.BAN.value,
            reason=ban_reason,
            created_by=ban_created_by,
            created_at=_NOW,
        )
    )
    await session.flush()
    return SensitiveHuman(
        principal_id=principal.id,
        display_name=display_name,
        ranked_handle=ranked_handle,
        email=email,
        email_hash=email_hash,
        ban_reason=ban_reason,
        ban_created_by=ban_created_by,
    )


def _setup_bodies(*, terminal: bool) -> list[dict[str, Any]]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    human_seats = {_CALLER_SEAT, _OTHER_HUMAN_SEAT}
    assignments = [
        {
            "public_player_id": seat.public_player_id,
            "seat_index": seat.seat_index,
            "role": seat.role.value,
            "faction": seat.faction.value,
            "seat_kind": (
                SeatKind.HUMAN.value if seat.public_player_id in human_seats else SeatKind.AI.value
            ),
        }
        for seat in seats
    ]
    bodies: list[dict[str, Any]] = [
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
            "payload": {"phase_kind": "DAY_VOTE", "day": 1, "round": 1},
        },
    ]
    if terminal:
        bodies.append(
            {
                "event_type": "GameTerminated",
                "sequence": 3,
                "phase": _PHASE,
                "visibility": "PUBLIC",
                "actor_player_id": None,
                "payload": {"winner": "TOWN", "reason": "all_mafia_eliminated"},
            }
        )
    return bodies


async def _append_events(session: AsyncSession, *, game_id: uuid.UUID, terminal: bool) -> None:
    log = EventLog()
    for body in _setup_bodies(terminal=terminal):
        stored = log.append(body)
        await events_repo.append_event(
            session,
            game_id=game_id,
            sequence=stored.sequence,
            event_type=body["event_type"],
            phase=body["phase"],
            visibility=body["visibility"],
            actor_player_id=body["actor_player_id"],
            payload=body["payload"],
            prev_event_hash=stored.prev_event_hash,
            event_hash=stored.event_hash,
        )


async def _seed_human_game(
    session: AsyncSession,
    *,
    caller_principal_id: uuid.UUID,
    other_principal_id: uuid.UUID,
    ai_build_id: uuid.UUID,
    terminal: bool,
    public_recent: bool,
) -> uuid.UUID:
    game = Game(
        gauntlet_id=None,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=_GAME_SEED,
        status="COMPLETED" if terminal else "RUNNING",
        terminal_result=(
            {"winner": "TOWN", "reason": "all_mafia_eliminated"} if terminal else None
        ),
        broadcast_state=(
            BroadcastState.RECENT.value if public_recent else BroadcastState.LIVE.value
        ),
        is_broadcastable=public_recent,
        identity_mode=IdentityMode.ANONYMOUS.value,
    )
    session.add(game)
    await session.flush()

    await _append_events(session, game_id=game.id, terminal=terminal)
    for seat in assign_roles(_GAME_SEED, mini7_v1):
        is_caller = seat.public_player_id == _CALLER_SEAT
        is_other_human = seat.public_player_id == _OTHER_HUMAN_SEAT
        is_human = is_caller or is_other_human
        session.add(
            GameSeat(
                game_id=game.id,
                public_player_id=seat.public_player_id,
                seat_index=seat.seat_index,
                agent_build_id=None if is_human else ai_build_id,
                seat_kind=SeatKind.HUMAN.value if is_human else SeatKind.AI.value,
                role=seat.role.value,
                faction=seat.faction.value,
                alive=True,
                occupant_principal_id=(
                    caller_principal_id
                    if is_caller
                    else other_principal_id
                    if is_other_human
                    else None
                ),
            )
        )

    await runtime_repo.upsert(
        session,
        game_id=game.id,
        phase=_PHASE,
        deadline_at=_DEADLINE,
        buffer_snapshot={},
        updated_at=_NOW,
    )
    await session.flush()
    return game.id


def _json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _assert_no_human_identity_values(text: str, humans: list[SensitiveHuman]) -> None:
    for human in humans:
        forbidden_values = [
            str(human.principal_id),
            human.display_name,
            human.ranked_handle,
            human.ranked_handle.lower(),
            human.email,
            human.email_hash,
            human.ban_reason,
            human.ban_created_by,
        ]
        for value in forbidden_values:
            assert value not in text


def _assert_no_ranked_or_pii_fields(text: str) -> None:
    forbidden_tokens = [
        "principal_id",
        "occupant_principal_id",
        "display_name",
        "ranked_handle",
        "ranked_handle_normalized",
        "email",
        "email_verified",
        "ranked_ban",
        "ranked_banned",
        "ranked_eligible",
        "eligibility",
        "ban_reason",
        "ban_kind",
        "created_by",
        "lifted_at",
        "expires_at",
    ]
    for token in forbidden_tokens:
        assert token not in text


def _assert_no_preterminal_identity_surface(text: str, humans: list[SensitiveHuman]) -> None:
    _assert_no_human_identity_values(text, humans)
    _assert_no_ranked_or_pii_fields(text)
    for token in [
        "agent_build_id",
        "model_name",
        "model_version",
        "provider",
        "seat_kind",
        "is_human",
        "takeover_agent_build_id",
        _AI_PROVIDER,
        _AI_MODEL,
        _AI_DISPLAY_NAME,
    ]:
        assert token not in text


def _assert_no_human_pii_or_ranked_flags(text: str, humans: list[SensitiveHuman]) -> None:
    _assert_no_human_identity_values(text, humans)
    for token in [
        "principal_id",
        "occupant_principal_id",
        "ranked_handle",
        "ranked_handle_normalized",
        "email",
        "email_verified",
        "ranked_ban",
        "ranked_banned",
        "ranked_eligible",
        "eligibility",
        "ban_reason",
        "ban_kind",
        "created_by",
        "lifted_at",
        "expires_at",
    ]:
        assert token not in text


async def test_preterminal_observation_hides_other_human_identity_and_ranked_flags(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, caller_id = await _guest(client)
    async with session_factory() as session, session.begin():
        caller = await _seed_sensitive_human(session, label="caller", principal_id=caller_id)
        other = await _seed_sensitive_human(session, label="other")
        ai_build_id = await _make_agent_build(session)
        game_id = await _seed_human_game(
            session,
            caller_principal_id=caller_id,
            other_principal_id=other.principal_id,
            ai_build_id=ai_build_id,
            terminal=False,
            public_recent=False,
        )

    resp = await client.get(
        f"/human/games/{game_id}/observation/stream",
        cookies={HUMAN_SESSION_COOKIE: token},
    )

    assert resp.status_code == 200, resp.text
    assert _CALLER_SEAT in resp.text
    assert _OTHER_HUMAN_SEAT in resp.text
    _assert_no_preterminal_identity_surface(resp.text, [caller, other])


async def test_preterminal_reveal_routes_return_only_status_errors_without_identity(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, caller_id = await _guest(client)
    raw_key = await _seed_key(session_factory, label="viewer")
    async with session_factory() as session, session.begin():
        caller = await _seed_sensitive_human(
            session, label="caller-pre-reveal", principal_id=caller_id
        )
        other = await _seed_sensitive_human(session, label="other-pre-reveal")
        ai_build_id = await _make_agent_build(session)
        game_id = await _seed_human_game(
            session,
            caller_principal_id=caller_id,
            other_principal_id=other.principal_id,
            ai_build_id=ai_build_id,
            terminal=False,
            public_recent=False,
        )

    public_resp = await client.get(f"/public/games/{game_id}/reveal", headers=_auth(raw_key))
    private_resp = await client.get(
        f"/human/games/{game_id}/reveal",
        cookies={HUMAN_SESSION_COOKIE: token},
    )

    assert public_resp.status_code == 404
    assert private_resp.status_code == 409
    _assert_no_preterminal_identity_surface(_json_text(public_resp.json()), [caller, other])
    _assert_no_preterminal_identity_surface(_json_text(private_resp.json()), [caller, other])


async def test_terminal_public_and_participant_reveals_show_ai_model_but_no_human_pii(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token, caller_id = await _guest(client)
    raw_key = await _seed_key(session_factory, label="viewer")
    async with session_factory() as session, session.begin():
        caller = await _seed_sensitive_human(
            session, label="caller-terminal", principal_id=caller_id
        )
        other = await _seed_sensitive_human(session, label="other-terminal")
        ai_build_id = await _make_agent_build(session)
        game_id = await _seed_human_game(
            session,
            caller_principal_id=caller_id,
            other_principal_id=other.principal_id,
            ai_build_id=ai_build_id,
            terminal=True,
            public_recent=True,
        )

    public_resp = await client.get(f"/public/games/{game_id}/reveal", headers=_auth(raw_key))
    private_resp = await client.get(
        f"/human/games/{game_id}/reveal",
        cookies={HUMAN_SESSION_COOKIE: token},
    )

    assert public_resp.status_code == 200, public_resp.text
    assert private_resp.status_code == 200, private_resp.text
    for body in [public_resp.json(), private_resp.json()]:
        text = _json_text(body)
        assert _AI_PROVIDER in text
        assert _AI_MODEL in text
        assert _AI_DISPLAY_NAME in text
        _assert_no_human_pii_or_ranked_flags(text, [caller, other])
