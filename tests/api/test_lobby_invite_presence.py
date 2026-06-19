"""Invite links, roster, ready-up, and presence for private lobbies (US-148).

Covers ``POST /lobbies/join/{invite_token}`` (guest- and account-joinable,
single-use-per-person via membership), roster read, ready-up, host
lock/kick/transfer, heartbeat presence with stale-member eviction, and the
idle/host-abandon auto-cancel lifecycle. Also asserts the member-scoped lobby
state channel streams roster/ready/presence identity-blind (never a per-seat
human/AI map) in anonymous mode.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.core.enums import LobbyStatus
from padrino.db.models import Lobby, LobbyMember


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


async def _friend_member_id(client: AsyncClient, lobby_id: str, host_token: str) -> str:
    roster = await client.get(
        f"/lobbies/{lobby_id}/roster", cookies={HUMAN_SESSION_COOKIE: host_token}
    )
    assert roster.status_code == 200, roster.text
    member_id: str = next(m["member_id"] for m in roster.json()["members"] if not m["is_host"])
    return member_id


async def _invite_token(client: AsyncClient, lobby_id: str, host_token: str) -> str:
    resp = await client.get(
        f"/lobbies/{lobby_id}",
        cookies={HUMAN_SESSION_COOKIE: host_token},
    )
    assert resp.status_code == 200, resp.text
    token: str = resp.json()["invite_token"]
    assert token
    return token


@pytest.mark.asyncio
async def test_guest_joins_via_invite_link(client: AsyncClient) -> None:
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


@pytest.mark.asyncio
async def test_join_unknown_invite_token_404(client: AsyncClient) -> None:
    friend = await _guest_token(client)
    resp = await client.post(
        "/lobbies/join/not-a-real-token",
        cookies={HUMAN_SESSION_COOKIE: friend},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_join_is_single_use_per_person(client: AsyncClient) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    invite = await _invite_token(client, lobby["id"], host)

    friend = await _guest_token(client)
    first = await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: friend})
    assert first.status_code == 200
    # Re-joining is idempotent: the same membership, not a second row.
    second = await client.post(f"/lobbies/join/{invite}", cookies={HUMAN_SESSION_COOKIE: friend})
    assert second.status_code == 200
    assert second.json()["member_count"] == 2


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
    assert body["composition"] == {"human_count": 1, "ai_count": 6, "total": 7}
    for member in body["members"]:
        assert set(member) == {"member_id", "is_host", "ready", "present"}

    # A non-member cannot read the roster.
    outsider = await _guest_token(client)
    denied = await client.get(
        f"/lobbies/{lobby['id']}/roster", cookies={HUMAN_SESSION_COOKIE: outsider}
    )
    assert denied.status_code == 404


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
    me = next(m for m in resp.json()["members"] if m["is_host"])
    assert me["ready"] is True

    unready = await client.post(
        f"/lobbies/{lobby['id']}/ready",
        json={"ready": False},
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    me = next(m for m in unready.json()["members"] if m["is_host"])
    assert me["ready"] is False


@pytest.mark.asyncio
async def test_host_lock(client: AsyncClient) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
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
    new_host = next(m for m in transferred.json()["members"] if m["is_host"])
    assert new_host["member_id"] == friend_member_id

    # The old host is now a non-host and may no longer transfer.
    denied = await client.post(
        f"/lobbies/{lobby['id']}/transfer/{friend_member_id}",
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert denied.status_code == 403


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
    assert lobby_id  # silence unused


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
    # Identity-blind: roster carries no per-seat human/AI map, no principal ids.
    forbidden = {"seat_kind", "principal_id", "agent_build_id", "is_human", "seats"}
    blob = json.dumps(frames)
    for key in forbidden:
        assert f'"{key}"' not in blob, key
    for member in snapshot["members"]:
        assert set(member) == {"member_id", "is_host", "ready", "present"}


@pytest.mark.asyncio
async def test_lobby_state_stream_requires_member(client: AsyncClient) -> None:
    host = await _guest_token(client)
    lobby = await _create_lobby(client, host)
    outsider = await _guest_token(client)
    resp = await client.get(
        f"/lobbies/{lobby['id']}/stream", cookies={HUMAN_SESSION_COOKIE: outsider}
    )
    assert resp.status_code == 404
