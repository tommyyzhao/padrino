"""Invite links, roster, ready-up, and presence for private lobbies (US-148).

Covers ``POST /lobbies/join/{invite_token}`` (guest- and account-joinable,
single-use-per-person via membership), roster read, ready-up, host
lock/kick/transfer, heartbeat presence with stale-member eviction, and the
idle/host-abandon auto-cancel lifecycle. Also asserts the member-scoped lobby
state channel streams roster/ready/presence identity-blind (never a per-seat
human/AI map) in anonymous mode.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.core.enums import LobbySeatKind, LobbyStatus
from padrino.db.models import (
    AgentBuild,
    HumanCostAdmission,
    HumanInferenceReservation,
    Lobby,
    LobbyMember,
    LobbySeat,
    ModelConfig,
    ModelProvider,
    PromptVersion,
)
from padrino.db.repositories import human_principals as principals_repo

_ROSTER_MEMBER_KEYS = {"member_id", "display_label", "is_self", "host", "ready", "present"}


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


async def _guest_token(client: AsyncClient) -> str:
    resp = await client.post("/human/guest")
    assert resp.status_code == 201
    return resp.cookies[HUMAN_SESSION_COOKIE]


async def _create_lobby(client: AsyncClient, token: str) -> dict[str, Any]:
    resp = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1"},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _make_agent_build(session: AsyncSession) -> AgentBuild:
    suffix = uuid.uuid4().hex
    provider = ModelProvider(
        name=f"cerebras-{suffix}", base_url=None, auth_secret_ref="CEREBRAS_API_KEY"
    )
    session.add(provider)
    await session.flush()
    model = ModelConfig(
        provider_id=provider.id,
        model_name=f"zai-glm-4.7-{suffix}",
        model_version=None,
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=4096,
        supports_structured_outputs=True,
    )
    session.add(model)
    prompt = PromptVersion(
        ruleset_id="mini7_v1",
        version=f"v1-{suffix}",
        system_prompt="play",
        developer_prompt="json",
        response_schema={"type": "object"},
        prompt_hash=f"hash-us328-{suffix}",
    )
    session.add(prompt)
    await session.flush()
    build = AgentBuild(
        display_name=f"cerebras/glm-4.7@{suffix}",
        model_config_id=model.id,
        prompt_version_id=prompt.id,
        adapter_version="2026.05",
        inference_params={"temperature": 0.7},
        active=True,
    )
    session.add(build)
    await session.flush()
    await session.commit()
    return build


async def _friend_member_id(client: AsyncClient, lobby_id: str, host_token: str) -> str:
    roster = await client.get(
        f"/lobbies/{lobby_id}/roster", cookies={HUMAN_SESSION_COOKIE: host_token}
    )
    assert roster.status_code == 200, roster.text
    member_id: str = next(m["member_id"] for m in roster.json()["members"] if not m["host"])
    return member_id


async def _create_lobby_with_legacy_build_reservation(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    host_token: str,
) -> tuple[dict[str, Any], uuid.UUID]:
    async with session_factory() as session:
        build = await _make_agent_build(session)
        build_id = build.id
    resp = await client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1"},
        cookies={HUMAN_SESSION_COOKIE: host_token},
    )
    assert resp.status_code == 201, resp.text
    lobby_id = uuid.UUID(resp.json()["id"])
    async with session_factory() as session:
        seat = await session.scalar(
            select(LobbySeat).where(LobbySeat.lobby_id == lobby_id, LobbySeat.seat_index == 1)
        )
        assert seat is not None
        seat.agent_build_id = build_id
        await session.commit()
    return resp.json(), build_id


async def _invite_token(client: AsyncClient, lobby_id: str, host_token: str) -> str:
    resp = await client.get(
        f"/lobbies/{lobby_id}",
        cookies={HUMAN_SESSION_COOKIE: host_token},
    )
    assert resp.status_code == 200, resp.text
    token: str = resp.json()["invite_token"]
    assert token
    return token


async def _set_ready(client: AsyncClient, lobby_id: str, token: str) -> None:
    resp = await client.post(
        f"/lobbies/{lobby_id}/ready",
        json={"ready": True},
        cookies={HUMAN_SESSION_COOKIE: token},
    )
    assert resp.status_code == 200, resp.text


async def _principal_id_for_token(
    session_factory: async_sessionmaker[AsyncSession], token: str
) -> uuid.UUID:
    async with session_factory() as session:
        record = await principals_repo.get_session_by_token(session, token)
        assert record is not None
        return record.principal_id


async def _lobby_seats(
    session_factory: async_sessionmaker[AsyncSession], lobby_id: str
) -> list[LobbySeat]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(LobbySeat)
                    .where(LobbySeat.lobby_id == uuid.UUID(lobby_id))
                    .order_by(LobbySeat.seat_index)
                )
            ).scalars()
        )


async def _member_count_for_principal(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    lobby_id: str,
    principal_id: uuid.UUID,
) -> int:
    async with session_factory() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(LobbyMember)
                    .where(
                        LobbyMember.lobby_id == uuid.UUID(lobby_id),
                        LobbyMember.principal_id == principal_id,
                    )
                )
            ).scalar_one()
        )


async def _live_join_slot_count(
    session_factory: async_sessionmaker[AsyncSession], principal_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(HumanCostAdmission)
                    .where(
                        HumanCostAdmission.principal_id == principal_id,
                        HumanCostAdmission.action == "join",
                        HumanCostAdmission.released_at.is_(None),
                    )
                )
            ).scalar_one()
        )


async def _live_inference_reservation_count(
    session_factory: async_sessionmaker[AsyncSession], principal_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        scope_key = principal_id.hex
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(HumanInferenceReservation)
                    .where(
                        HumanInferenceReservation.scope_key == scope_key,
                        HumanInferenceReservation.released_at.is_(None),
                    )
                )
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_guest_joins_via_invite_link(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    invite = await _invite_token(client, lobby["id"], host)

    friend = await _guest_token(client)
    resp = await client.post(
        f"/lobbies/join/{invite}",
        cookies={HUMAN_SESSION_COOKIE: friend},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["member_count"] == 2
    assert resp.json()["composition"] == {"human_count": 2, "ai_count": 5, "total": 7}

    seats = await _lobby_seats(session_factory, lobby["id"])
    human_seats = [seat for seat in seats if seat.seat_kind == LobbySeatKind.HUMAN.value]
    assert len(human_seats) == 2
    friend_member = next(seat for seat in human_seats if seat.seat_index != 0)
    assert friend_member.member_id is not None
    assert friend_member.agent_build_id is None


@pytest.mark.asyncio
async def test_waiting_room_projection_has_safe_ai_cards_and_launch_reasons(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The waiting room gets named model cards and public readiness tokens only."""
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    lobby_id = uuid.UUID(lobby["id"])

    # This is intentionally richer than the browser projection.  The route must
    # retain only the model slug/count and must never disclose frozen routing,
    # ownership, or catalog identifiers alongside the card.
    async with session_factory() as session, session.begin():
        seat = await session.scalar(
            select(LobbySeat).where(
                LobbySeat.lobby_id == lobby_id,
                LobbySeat.seat_index == 1,
            )
        )
        assert seat is not None
        seat.model_selection = {
            "canonical_slug": "openai/waiting-room-model",
            "catalog_snapshot_id": str(uuid.uuid4()),
            "catalog_entry_id": str(uuid.uuid4()),
            "funding_owner_ref": "principal:must-not-reach-browser",
            "routing_preferences": {"order": ["provider-a"]},
        }

    summary = await client.get(f"/lobbies/{lobby['id']}", cookies={HUMAN_SESSION_COOKIE: host})
    assert summary.status_code == 200, summary.text
    assert summary.json()["ai_roster"] == [
        {"canonical_slug": "openai/waiting-room-model", "count": 1}
    ]
    rendered = summary.text
    assert "funding_owner_ref" not in rendered
    assert "catalog_snapshot_id" not in rendered
    assert "provider-a" not in rendered

    roster = await client.get(
        f"/lobbies/{lobby['id']}/roster", cookies={HUMAN_SESSION_COOKIE: host}
    )
    assert roster.status_code == 200, roster.text
    assert roster.json()["launch_readiness"] == {
        "allowed": False,
        "reasons": ["not_ready_member"],
    }


@pytest.mark.asyncio
async def test_join_unknown_invite_token_404(client: AsyncClient) -> None:
    friend = await _guest_token(client)
    resp = await client.post(
        "/lobbies/join/not-a-real-token",
        cookies={HUMAN_SESSION_COOKIE: friend},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_join_is_single_use_per_person(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    invite = await _invite_token(client, lobby["id"], host)

    friend = await _guest_token(client)
    friend_principal_id = await _principal_id_for_token(session_factory, friend)
    first = await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: friend})
    assert first.status_code == 200
    seats_after_first = await _lobby_seats(session_factory, lobby["id"])
    friend_seats_after_first = [
        seat
        for seat in seats_after_first
        if seat.seat_kind == LobbySeatKind.HUMAN.value and seat.seat_index != 0
    ]
    assert len(friend_seats_after_first) == 1
    first_member_id = friend_seats_after_first[0].member_id
    assert first_member_id is not None
    assert await _live_join_slot_count(session_factory, friend_principal_id) == 1

    # Re-joining is idempotent: the same membership, not a second row.
    second = await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: friend})
    assert second.status_code == 200
    assert second.json()["member_count"] == 2
    assert second.json()["composition"] == {"human_count": 2, "ai_count": 5, "total": 7}

    seats_after_second = await _lobby_seats(session_factory, lobby["id"])
    assert [
        seat.member_id
        for seat in seats_after_second
        if seat.seat_kind == LobbySeatKind.HUMAN.value and seat.seat_index != 0
    ] == [first_member_id]
    assert await _live_join_slot_count(session_factory, friend_principal_id) == 1


@pytest.mark.asyncio
async def test_join_requires_human_session(client: AsyncClient) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    invite = await _invite_token(client, lobby["id"], host)
    resp = await client.post(f"/lobbies/join/{invite}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_roster_read_member_scoped(client: AsyncClient) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    invite = await _invite_token(client, lobby["id"], host)
    friend = await _guest_token(client)
    await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: friend})

    roster = await client.get(
        f"/lobbies/{lobby['id']}/roster", cookies={HUMAN_SESSION_COOKIE: host}
    )
    assert roster.status_code == 200
    body = roster.json()
    assert len(body["members"]) == 2
    # Identity-blind: composition is counts only, never a per-seat human/AI map.
    assert body["composition"] == {"human_count": 2, "ai_count": 5, "total": 7}
    for member in body["members"]:
        assert set(member) == _ROSTER_MEMBER_KEYS

    # A non-member cannot read the roster.
    outsider = await _guest_token(client)
    denied = await client.get(
        f"/lobbies/{lobby['id']}/roster", cookies={HUMAN_SESSION_COOKIE: outsider}
    )
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_roster_display_labels_are_lobby_local_and_do_not_leak_identity(
    client: AsyncClient,
) -> None:
    host = await _guest_token(client)
    renamed = await client.patch(
        "/human/me",
        json={"display_name": "Alex"},
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert renamed.status_code == 200, renamed.text
    host_principal_id = renamed.json()["principal_id"]
    lobby = await _create_lobby(client, host)
    invite = await _invite_token(client, lobby["id"], host)

    friend = await _guest_token(client)
    renamed_friend = await client.patch(
        "/human/me",
        json={"display_name": "Alex"},
        cookies={HUMAN_SESSION_COOKIE: friend},
    )
    assert renamed_friend.status_code == 200, renamed_friend.text
    friend_principal_id = renamed_friend.json()["principal_id"]
    joined = await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: friend})
    assert joined.status_code == 200, joined.text

    unnamed = await _guest_token(client)
    unnamed_me = await client.get("/human/me", cookies={HUMAN_SESSION_COOKIE: unnamed})
    assert unnamed_me.status_code == 200, unnamed_me.text
    unnamed_principal_id = unnamed_me.json()["principal_id"]
    joined_unnamed = await client.post(
        f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: unnamed}
    )
    assert joined_unnamed.status_code == 200, joined_unnamed.text

    roster = await client.get(
        f"/lobbies/{lobby['id']}/roster",
        cookies={HUMAN_SESSION_COOKIE: friend},
    )

    assert roster.status_code == 200, roster.text
    body = roster.json()
    assert body["composition"] == {"human_count": 3, "ai_count": 4, "total": 7}
    assert [set(member) for member in body["members"]] == [_ROSTER_MEMBER_KEYS] * 3
    assert [member["display_label"] for member in body["members"]] == [
        "Alex #1",
        "Alex #2",
        "Player 3",
    ]
    assert [member["host"] for member in body["members"]] == [True, False, False]
    assert [member["is_self"] for member in body["members"]] == [False, True, False]

    member_ids = {member["member_id"] for member in body["members"]}
    assert member_ids.isdisjoint({host_principal_id, friend_principal_id, unnamed_principal_id})

    encoded = json.dumps(body, sort_keys=True)
    forbidden_tokens = {
        "principal_id",
        "email",
        "public_player_id",
        "seat_index",
        "seat_kind",
        "agent_build_id",
        "provider",
        "model_name",
        "model_config_id",
    }
    for token in forbidden_tokens:
        assert f'"{token}"' not in encoded
    assert host_principal_id not in encoded
    assert friend_principal_id not in encoded
    assert unnamed_principal_id not in encoded

    host_after = await client.get("/human/me", cookies={HUMAN_SESSION_COOKIE: host})
    assert host_after.status_code == 200, host_after.text
    assert host_after.json()["display_name"] == "Alex"


@pytest.mark.asyncio
async def test_ready_up(client: AsyncClient) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    resp = await client.post(
        f"/lobbies/{lobby['id']}/ready",
        json={"ready": True},
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert resp.status_code == 200
    me = next(m for m in resp.json()["members"] if m["host"])
    assert me["ready"] is True

    unready = await client.post(
        f"/lobbies/{lobby['id']}/ready",
        json={"ready": False},
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    me = next(m for m in unready.json()["members"] if m["host"])
    assert me["ready"] is False


@pytest.mark.asyncio
async def test_host_lock(client: AsyncClient) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    await _set_ready(client, lobby["id"], host)
    resp = await client.post(f"/lobbies/{lobby['id']}/lock", cookies={HUMAN_SESSION_COOKIE: host})
    assert resp.status_code == 200
    assert resp.json()["status"] == LobbyStatus.LOCKED.value

    # A non-host cannot lock.
    friend = await _guest_token(client)
    invite = await _invite_token(client, lobby["id"], host)
    # Lobby is now LOCKED; even joining is closed, but use a fresh OPEN lobby.
    lobby2 = await _create_lobby(client, host)
    invite2 = await _invite_token(client, lobby2["id"], host)
    await client.post(f"/lobbies/join/{invite2}", cookies={HUMAN_SESSION_COOKIE: friend})
    denied = await client.post(
        f"/lobbies/{lobby2['id']}/lock", cookies={HUMAN_SESSION_COOKIE: friend}
    )
    assert denied.status_code == 403
    assert invite  # silence unused


@pytest.mark.asyncio
async def test_host_lock_requires_every_member_ready(client: AsyncClient) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)

    denied = await client.post(f"/lobbies/{lobby['id']}/lock", cookies={HUMAN_SESSION_COOKIE: host})

    assert denied.status_code == 409
    assert denied.json()["detail"] == "not_ready_member"


@pytest.mark.asyncio
async def test_host_kick(client: AsyncClient) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    invite = await _invite_token(client, lobby["id"], host)
    friend = await _guest_token(client)
    await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: friend})
    friend_member_id = await _friend_member_id(client, lobby["id"], host)

    kicked = await client.post(
        f"/lobbies/{lobby['id']}/kick/{friend_member_id}",
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert kicked.status_code == 200
    assert kicked.json()["member_count"] == 1

    # The kicked friend can no longer read the lobby.
    denied = await client.get(
        f"/lobbies/{lobby['id']}/roster", cookies={HUMAN_SESSION_COOKIE: friend}
    )
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_kick_releases_member_seat_admission_and_allows_refill(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    host = await _guest_token(client)
    lobby, build_id = await _create_lobby_with_legacy_build_reservation(
        client, session_factory, host
    )
    invite = await _invite_token(client, lobby["id"], host)
    friend = await _guest_token(client)
    friend_principal_id = await _principal_id_for_token(session_factory, friend)
    joined = await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: friend})
    assert joined.status_code == 200, joined.text
    friend_member_id = await _friend_member_id(client, lobby["id"], host)

    kicked = await client.post(
        f"/lobbies/{lobby['id']}/kick/{friend_member_id}",
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert kicked.status_code == 200, kicked.text
    assert kicked.json()["member_count"] == 1
    assert kicked.json()["composition"] == {"human_count": 1, "ai_count": 6, "total": 7}

    seats = await _lobby_seats(session_factory, lobby["id"])
    released = seats[1]
    assert released.seat_kind == LobbySeatKind.AI.value
    assert released.member_id is None
    assert released.agent_build_id == build_id
    assert await _live_join_slot_count(session_factory, friend_principal_id) == 0
    assert await _live_inference_reservation_count(session_factory, friend_principal_id) == 0

    replacement = await _guest_token(client)
    refill = await client.post(
        f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: replacement}
    )
    assert refill.status_code == 200, refill.text
    assert refill.json()["composition"] == {"human_count": 2, "ai_count": 5, "total": 7}
    seats_after_refill = await _lobby_seats(session_factory, lobby["id"])
    reclaimed = seats_after_refill[1]
    assert reclaimed.seat_kind == LobbySeatKind.HUMAN.value
    assert reclaimed.member_id is not None
    assert reclaimed.agent_build_id == build_id


@pytest.mark.asyncio
async def test_member_leave_releases_seat_and_admissions(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    invite = await _invite_token(client, lobby["id"], host)
    friend = await _guest_token(client)
    friend_principal_id = await _principal_id_for_token(session_factory, friend)
    joined = await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: friend})
    assert joined.status_code == 200, joined.text

    left = await client.post(
        f"/lobbies/{lobby['id']}/leave", cookies={HUMAN_SESSION_COOKIE: friend}
    )
    assert left.status_code == 200, left.text
    assert left.json()["member_count"] == 1
    assert left.json()["composition"] == {"human_count": 1, "ai_count": 6, "total": 7}

    seats = await _lobby_seats(session_factory, lobby["id"])
    assert seats[1].seat_kind == LobbySeatKind.AI.value
    assert seats[1].member_id is None
    assert await _live_join_slot_count(session_factory, friend_principal_id) == 0
    assert await _live_inference_reservation_count(session_factory, friend_principal_id) == 0


@pytest.mark.asyncio
async def test_host_must_transfer_before_leaving_multi_member_lobby(client: AsyncClient) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    invite = await _invite_token(client, lobby["id"], host)
    friend = await _guest_token(client)
    joined = await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: friend})
    assert joined.status_code == 200, joined.text

    denied = await client.post(
        f"/lobbies/{lobby['id']}/leave", cookies={HUMAN_SESSION_COOKIE: host}
    )
    assert denied.status_code == 409
    assert denied.json()["detail"] == "host_must_transfer_before_leave"


@pytest.mark.asyncio
async def test_host_transfer(client: AsyncClient) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    invite = await _invite_token(client, lobby["id"], host)
    friend = await _guest_token(client)
    await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: friend})
    friend_member_id = await _friend_member_id(client, lobby["id"], host)

    transferred = await client.post(
        f"/lobbies/{lobby['id']}/transfer/{friend_member_id}",
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert transferred.status_code == 200
    new_host = next(m for m in transferred.json()["members"] if m["host"])
    assert new_host["member_id"] == friend_member_id

    # The old host is now a non-host and may no longer transfer.
    denied = await client.post(
        f"/lobbies/{lobby['id']}/transfer/{friend_member_id}",
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_host_transfer_does_not_move_or_duplicate_seat_ownership(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    invite = await _invite_token(client, lobby["id"], host)
    friend = await _guest_token(client)
    await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: friend})
    friend_member_id = await _friend_member_id(client, lobby["id"], host)
    before = [
        (seat.seat_index, seat.seat_kind, seat.member_id, seat.agent_build_id)
        for seat in await _lobby_seats(session_factory, lobby["id"])
    ]

    transferred = await client.post(
        f"/lobbies/{lobby['id']}/transfer/{friend_member_id}",
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert transferred.status_code == 200, transferred.text

    after = [
        (seat.seat_index, seat.seat_kind, seat.member_id, seat.agent_build_id)
        for seat in await _lobby_seats(session_factory, lobby["id"])
    ]
    assert after == before
    human_member_ids = [
        member_id for _, kind, member_id, _ in after if kind == LobbySeatKind.HUMAN.value
    ]
    assert len(human_member_ids) == len(set(human_member_ids)) == 2


@pytest.mark.asyncio
async def test_heartbeat_presence_eviction(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    invite = await _invite_token(client, lobby["id"], host)
    friend = await _guest_token(client)
    await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: friend})
    friend_member_id = uuid.UUID(await _friend_member_id(client, lobby["id"], host))

    # Host heartbeats now; the friend's presence is forced stale in the past.
    await client.post(f"/lobbies/{lobby['id']}/heartbeat", cookies={HUMAN_SESSION_COOKIE: host})
    lobby_id = uuid.UUID(lobby["id"])
    async with session_factory() as session, session.begin():
        member = await session.get(LobbyMember, friend_member_id)
        assert member is not None
        member.last_seen_at = datetime.now(UTC) - timedelta(hours=1)

    # The host's next roster read evicts the stale friend.
    roster = await client.get(
        f"/lobbies/{lobby['id']}/roster", cookies={HUMAN_SESSION_COOKIE: host}
    )
    assert roster.status_code == 200
    member_ids = {m["member_id"] for m in roster.json()["members"]}
    assert str(friend_member_id) not in member_ids
    assert roster.json()["member_count"] == 1
    assert roster.json()["composition"] == {"human_count": 1, "ai_count": 6, "total": 7}
    seats = await _lobby_seats(session_factory, lobby["id"])
    assert seats[1].seat_kind == LobbySeatKind.AI.value
    assert seats[1].member_id is None
    assert lobby_id  # silence unused


@pytest.mark.asyncio
async def test_concurrent_kick_and_leave_release_lifecycle_once(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    invite = await _invite_token(client, lobby["id"], host)
    friend = await _guest_token(client)
    friend_principal_id = await _principal_id_for_token(session_factory, friend)
    joined = await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: friend})
    assert joined.status_code == 200, joined.text
    friend_member_id = await _friend_member_id(client, lobby["id"], host)

    responses = await asyncio.gather(
        client.post(
            f"/lobbies/{lobby['id']}/kick/{friend_member_id}",
            cookies={HUMAN_SESSION_COOKIE: host},
        ),
        client.post(f"/lobbies/{lobby['id']}/leave", cookies={HUMAN_SESSION_COOKIE: friend}),
    )

    assert sorted(resp.status_code for resp in responses) in ([200, 200], [200, 404])
    seats = await _lobby_seats(session_factory, lobby["id"])
    assert seats[1].seat_kind == LobbySeatKind.AI.value
    assert seats[1].member_id is None
    assert await _live_join_slot_count(session_factory, friend_principal_id) == 0
    assert await _live_inference_reservation_count(session_factory, friend_principal_id) == 0

    retried = await client.post(
        f"/lobbies/{lobby['id']}/kick/{friend_member_id}",
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert retried.status_code == 200, retried.text
    seats_after_retry = await _lobby_seats(session_factory, lobby["id"])
    assert [(seat.seat_kind, seat.member_id) for seat in seats_after_retry] == [
        (seat.seat_kind, seat.member_id) for seat in seats
    ]


@pytest.mark.asyncio
async def test_idle_host_abandon_auto_cancel(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    lobby_id = uuid.UUID(lobby["id"])

    # Force the lobby idle: drag its updated_at + all members' presence far back.
    async with session_factory() as session, session.begin():
        row = await session.get(Lobby, lobby_id)
        assert row is not None
        row.updated_at = datetime.now(UTC) - timedelta(hours=2)

    # The host's next read observes the idle lobby auto-cancelled (CLOSED).
    roster = await client.get(
        f"/lobbies/{lobby['id']}/roster", cookies={HUMAN_SESSION_COOKIE: host}
    )
    assert roster.status_code == 200
    assert roster.json()["status"] == LobbyStatus.CLOSED.value


@pytest.mark.asyncio
async def test_lobby_state_stream_identity_blind(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from padrino.settings import get_settings

    monkeypatch.setenv("PADRINO_LOBBY_STREAM_IDLE_TIMEOUT_MS", "10")
    monkeypatch.setenv("PADRINO_LOBBY_STREAM_POLL_MS", "1")
    get_settings.cache_clear()
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)

    resp = await client.get(f"/lobbies/{lobby['id']}/stream", cookies={HUMAN_SESSION_COOKIE: host})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = [
        json.loads(line[len("data:") :].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:")
    ]
    assert frames, resp.text
    snapshot = frames[0]
    assert snapshot["type"] == "lobby_state"
    assert snapshot["status"] == LobbyStatus.OPEN.value
    assert snapshot["composition"] == {"human_count": 1, "ai_count": 6, "total": 7}
    assert snapshot["launch_readiness"] == {
        "allowed": False,
        "reasons": ["not_ready_member"],
    }
    # Identity-blind: roster carries no per-seat human/AI map, no principal ids.
    forbidden = {"seat_kind", "principal_id", "agent_build_id", "is_human", "seats"}
    blob = json.dumps(frames)
    for key in forbidden:
        assert f'"{key}"' not in blob, key
    for member in snapshot["members"]:
        assert set(member) == _ROSTER_MEMBER_KEYS


@pytest.mark.asyncio
async def test_lobby_state_stream_requires_member(client: AsyncClient) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    outsider = await _guest_token(client)
    resp = await client.get(
        f"/lobbies/{lobby['id']}/stream", cookies={HUMAN_SESSION_COOKIE: outsider}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_full_lobby_join_fails_without_unseated_member_or_admission(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    invite = await _invite_token(client, lobby["id"], host)

    for _ in range(6):
        token = await _guest_token(client)
        joined = await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: token})
        assert joined.status_code == 200, joined.text

    denied_token = await _guest_token(client)
    denied_principal_id = await _principal_id_for_token(session_factory, denied_token)
    denied = await client.post(
        f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: denied_token}
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"] == "lobby_full"

    seats = await _lobby_seats(session_factory, lobby["id"])
    assert len([seat for seat in seats if seat.seat_kind == LobbySeatKind.HUMAN.value]) == 7
    assert all(seat.member_id is not None for seat in seats)
    assert (
        await _member_count_for_principal(
            session_factory, lobby_id=lobby["id"], principal_id=denied_principal_id
        )
        == 0
    )
    assert await _live_join_slot_count(session_factory, denied_principal_id) == 0
    assert await _live_inference_reservation_count(session_factory, denied_principal_id) == 0


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_concurrent_last_slot_joins_one_success_one_lobby_full_on_postgres(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    invite = await _invite_token(client, lobby["id"], host)

    for _ in range(5):
        token = await _guest_token(client)
        joined = await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: token})
        assert joined.status_code == 200, joined.text

    contenders = [await _guest_token(client), await _guest_token(client)]
    responses = await asyncio.gather(
        *[
            client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: token})
            for token in contenders
        ]
    )

    assert sorted(resp.status_code for resp in responses) == [200, 409]
    denied = next(resp for resp in responses if resp.status_code == 409)
    assert denied.json()["detail"] == "lobby_full"
    seats = await _lobby_seats(session_factory, lobby["id"])
    assert len([seat for seat in seats if seat.seat_kind == LobbySeatKind.HUMAN.value]) == 7
    assert len([seat for seat in seats if seat.seat_kind == LobbySeatKind.AI.value]) == 0
