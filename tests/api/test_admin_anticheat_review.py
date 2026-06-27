"""US-313: admin anti-cheat review routes."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_ADMIN, SCOPE_SPECTATOR, generate_raw_key
from padrino.core.rulesets import mini7_v1
from padrino.db.models import Game, HumanRating, HumanRatingEvent, RankedFlag, RankedGameSignal
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.db.repositories import games, human_principals, leagues
from padrino.db.repositories import ranked_flags as ranked_flags_repo
from padrino.ratings.openskill_service import INITIAL_MU, INITIAL_SIGMA, SCOPE_GLOBAL

_NOW = datetime(2026, 6, 27, 16, 0, tzinfo=UTC)
_RAW_IP = "203.0.113.42"
_IP_HASH_A = "a" * 64
_IP_HASH_B = "b" * 64
_DEVICE_HASH = "c" * 64
_DISPLAY_NAME = "Alice Realname AntiCheat Sentinel"


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _headers(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def _api_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scopes: list[str],
) -> str:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(session, raw_key=raw, scopes=scopes, label="test")
    return raw


async def _seed_principal(
    session: AsyncSession,
    *,
    display_name: str,
    ranked_handle: str,
) -> uuid.UUID:
    principal = await human_principals.create_principal(
        session,
        kind=human_principals.PRINCIPAL_KIND_ACCOUNT,
        display_name=display_name,
    )
    await human_principals.claim_ranked_handle(
        session,
        principal.id,
        candidate=ranked_handle,
        blocklist=(),
        reserved=(),
        now=_NOW,
    )
    return principal.id


async def _seed_game_signal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    game_suffix: int,
    source_ip_hash: str,
    winner: str,
    faction: str,
    created_at: datetime,
) -> uuid.UUID:
    league = await leagues.get_or_create_humans_included(
        session,
        ruleset_id=mini7_v1.RULESET_ID,
        ranked=True,
    )
    game = await games.create(
        session,
        ruleset_id=mini7_v1.RULESET_ID,
        game_seed=f"admin-anticheat-{game_suffix}",
        status="COMPLETED",
    )
    game.ranked_source_ip_hash = source_ip_hash
    game.completed_at = created_at
    session.add(
        RankedGameSignal(
            game_id=game.id,
            human_principal_id=principal_id,
            source_ip_hash=source_ip_hash,
            session_fingerprint_hash=_DEVICE_HASH,
            winner=winner,
            faction=faction,
            signals={"action_latency_ms": 512, "raw_ip": _RAW_IP},
            created_at=created_at,
        )
    )
    if game_suffix == 1:
        session.add(
            HumanRating(
                league_id=league.id,
                human_player_id=str(principal_id),
                scope_type=SCOPE_GLOBAL,
                scope_value="global",
                mu=INITIAL_MU + 1,
                sigma=INITIAL_SIGMA,
                conservative_score=(INITIAL_MU + 1) - 3.0 * INITIAL_SIGMA,
                games=1,
                last_game_at=created_at,
                updated_at=created_at,
            )
        )
    session.add(
        HumanRatingEvent(
            league_id=league.id,
            game_id=game.id,
            human_player_id=str(principal_id),
            public_player_id="P03",
            scope_type=SCOPE_GLOBAL,
            scope_value="global",
            before_mu=INITIAL_MU,
            before_sigma=INITIAL_SIGMA,
            after_mu=INITIAL_MU + game_suffix,
            after_sigma=INITIAL_SIGMA - 1,
            created_at=created_at,
        )
    )
    return game.id


def _flag(
    *,
    kind: str,
    principal_id: uuid.UUID,
    severity: str,
    evidence: dict[str, Any],
    created_at: datetime,
    row_id: uuid.UUID,
) -> RankedFlag:
    return RankedFlag(
        id=row_id,
        kind=kind,
        principal_id=str(principal_id),
        severity=severity,
        evidence=evidence,
        evidence_hash=ranked_flags_repo.evidence_hash(evidence),
        created_at=created_at,
    )


async def _seed_review_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as session, session.begin():
        principal_id = await _seed_principal(
            session,
            display_name=_DISPLAY_NAME,
            ranked_handle="RankedAlpha",
        )
        other_id = await _seed_principal(
            session,
            display_name="Bob Hidden Display Sentinel",
            ranked_handle="RankedBeta",
        )
        first_game_id = await _seed_game_signal(
            session,
            principal_id=principal_id,
            game_suffix=1,
            source_ip_hash=_IP_HASH_A,
            winner="TOWN",
            faction="TOWN",
            created_at=_NOW - timedelta(minutes=3),
        )
        await _seed_game_signal(
            session,
            principal_id=principal_id,
            game_suffix=2,
            source_ip_hash=_IP_HASH_A,
            winner="MAFIA",
            faction="TOWN",
            created_at=_NOW - timedelta(minutes=2),
        )
        await _seed_game_signal(
            session,
            principal_id=principal_id,
            game_suffix=3,
            source_ip_hash=_IP_HASH_B,
            winner="MAFIA",
            faction="MAFIA",
            created_at=_NOW - timedelta(minutes=1),
        )
        session.add_all(
            [
                _flag(
                    kind="same_ip_cluster",
                    principal_id=principal_id,
                    severity="medium",
                    evidence={
                        "source_ip_hash": _IP_HASH_A,
                        "principal_ids": [str(principal_id), str(other_id)],
                        "raw_ip": _RAW_IP,
                    },
                    created_at=_NOW - timedelta(seconds=3),
                    row_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                ),
                _flag(
                    kind="same_ip_cluster",
                    principal_id=other_id,
                    severity="medium",
                    evidence={
                        "source_ip_hash": _IP_HASH_A,
                        "principal_ids": [str(principal_id), str(other_id)],
                    },
                    created_at=_NOW - timedelta(seconds=2),
                    row_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
                ),
                _flag(
                    kind="bot_cadence",
                    principal_id=principal_id,
                    severity="low",
                    evidence={"average_latency_ms": 512},
                    created_at=_NOW - timedelta(seconds=1),
                    row_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
                ),
            ]
        )
        return principal_id, first_game_id


async def _row_snapshot(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    async with session_factory() as session:
        games_rows = list((await session.execute(select(Game).order_by(Game.id))).scalars())
        rating_rows = list(
            (await session.execute(select(HumanRating).order_by(HumanRating.id))).scalars()
        )
        event_rows = list(
            (
                await session.execute(select(HumanRatingEvent).order_by(HumanRatingEvent.id))
            ).scalars()
        )
        flag_rows = list(
            (await session.execute(select(RankedFlag).order_by(RankedFlag.id))).scalars()
        )
        signal_rows = list(
            (
                await session.execute(select(RankedGameSignal).order_by(RankedGameSignal.id))
            ).scalars()
        )
    return {
        "games": [
            {
                "id": str(row.id),
                "status": row.status,
                "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                "ranked_source_ip_hash": row.ranked_source_ip_hash,
            }
            for row in games_rows
        ],
        "ratings": [
            {
                "id": str(row.id),
                "mu": row.mu,
                "sigma": row.sigma,
                "games": row.games,
                "updated_at": row.updated_at.isoformat(),
            }
            for row in rating_rows
        ],
        "events": [
            {
                "id": str(row.id),
                "after_mu": row.after_mu,
                "after_sigma": row.after_sigma,
                "created_at": row.created_at.isoformat(),
            }
            for row in event_rows
        ],
        "flags": [
            {
                "id": str(row.id),
                "kind": row.kind,
                "severity": row.severity,
                "evidence": row.evidence,
                "evidence_hash": row.evidence_hash,
                "created_at": row.created_at.isoformat(),
            }
            for row in flag_rows
        ],
        "signals": [
            {
                "id": str(row.id),
                "game_id": str(row.game_id),
                "source_ip_hash": row.source_ip_hash,
                "signals": row.signals,
                "created_at": row.created_at.isoformat(),
            }
            for row in signal_rows
        ],
    }


def _assert_no_private_identity_leak(body: dict[str, Any]) -> None:
    rendered = json.dumps(body, sort_keys=True)
    assert _DISPLAY_NAME not in rendered
    assert _RAW_IP not in rendered
    assert "RankedAlpha" in rendered
    assert _IP_HASH_A in rendered


@pytest.mark.asyncio
async def test_anticheat_flags_requires_admin_scope(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    spectator = await _api_key(session_factory, scopes=[SCOPE_SPECTATOR])

    unauthenticated = await client.get("/admin/anticheat/flags")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"] == "authentication_required"

    non_admin = await client.get(
        "/admin/anticheat/flags",
        headers=_headers(spectator),
    )
    assert non_admin.status_code == 403
    assert non_admin.json()["detail"] == "insufficient_scope"


@pytest.mark.asyncio
async def test_admin_lists_filtered_flags_with_keyset_pagination_and_no_leaks(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = await _api_key(session_factory, scopes=[SCOPE_ADMIN])
    principal_id, _ = await _seed_review_rows(session_factory)

    first = await client.get(
        "/admin/anticheat/flags",
        headers=_headers(admin),
        params={"kind": "same_ip_cluster", "severity": "medium", "limit": 1},
    )

    assert first.status_code == 200, first.text
    first_body = first.json()
    assert len(first_body["items"]) == 1
    assert first_body["items"][0]["principal_id"] == str(principal_id)
    assert first_body["items"][0]["ranked_handle"] == "RankedAlpha"
    assert first_body["items"][0]["evidence"]["raw_ip"] == "[redacted]"
    assert first_body["next_cursor"] is not None
    _assert_no_private_identity_leak(first_body)

    second = await client.get(
        "/admin/anticheat/flags",
        headers=_headers(admin),
        params={
            "kind": "same_ip_cluster",
            "severity": "medium",
            "limit": 1,
            "cursor": first_body["next_cursor"],
        },
    )

    assert second.status_code == 200, second.text
    second_body = second.json()
    assert len(second_body["items"]) == 1
    assert second_body["items"][0]["ranked_handle"] == "RankedBeta"
    assert second_body["next_cursor"] is None

    filtered = await client.get(
        "/admin/anticheat/flags",
        headers=_headers(admin),
        params={"principal_id": str(principal_id), "severity": "low"},
    )
    assert filtered.status_code == 200, filtered.text
    assert [item["kind"] for item in filtered.json()["items"]] == ["bot_cadence"]


@pytest.mark.asyncio
async def test_admin_principal_rollup_returns_ranked_games_flags_ips_and_win_rate(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = await _api_key(session_factory, scopes=[SCOPE_ADMIN])
    principal_id, _ = await _seed_review_rows(session_factory)

    response = await client.get(
        f"/admin/anticheat/principals/{principal_id}",
        headers=_headers(admin),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["principal_id"] == str(principal_id)
    assert body["ranked_handle"] == "RankedAlpha"
    assert body["ranked_game_count"] == 3
    assert body["distinct_source_ip_hash_count"] == 2
    assert body["win_rate"] == pytest.approx(2 / 3)
    assert [flag["kind"] for flag in body["flags"]] == ["same_ip_cluster", "bot_cadence"]
    assert body["flags"][0]["evidence"]["raw_ip"] == "[redacted]"
    _assert_no_private_identity_leak(body)


@pytest.mark.asyncio
async def test_admin_game_signal_inspection_is_read_only_and_no_leak(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = await _api_key(session_factory, scopes=[SCOPE_ADMIN])
    _, game_id = await _seed_review_rows(session_factory)
    before = await _row_snapshot(session_factory)

    response = await client.get(
        f"/admin/anticheat/games/{game_id}/signals",
        headers=_headers(admin),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["game_id"] == str(game_id)
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["ranked_handle"] == "RankedAlpha"
    assert item["source_ip_hash"] == _IP_HASH_A
    assert item["session_fingerprint_hash"] == _DEVICE_HASH
    assert item["signals"]["raw_ip"] == "[redacted]"
    _assert_no_private_identity_leak(body)

    after = await _row_snapshot(session_factory)
    assert after == before
