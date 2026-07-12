"""US-334: repository rules-snapshot persistence and validation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.enums import LobbyStatus
from padrino.core.rulesets import get_ruleset, mini7_v1
from padrino.core.rulesets.snapshot import (
    canonical_rules_dict,
    canonical_rules_hash,
    snapshot_from_ruleset,
)
from padrino.db.models import Game, Lobby
from padrino.db.repositories import games, human_principals, leagues, lobbies
from padrino.db.rules_snapshots import RulesSnapshotValidationError

FORBIDDEN_SNAPSHOT_KEYS = {
    "auth_secret_ref",
    "balance",
    "created_at",
    "credential",
    "funding",
    "identity_mode",
    "principal_id",
    "provider",
    "public_player_id",
    "routing",
    "timestamp",
    "updated_at",
}


def _walk_keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        mapping_keys = {str(key) for key in value}
        for item in value.values():
            mapping_keys |= _walk_keys(item)
        return mapping_keys
    if isinstance(value, list | tuple):
        sequence_keys: set[str] = set()
        for item in value:
            sequence_keys |= _walk_keys(item)
        return sequence_keys
    return set()


async def test_games_create_persists_canonical_builtin_rules_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    expected_snapshot = snapshot_from_ruleset(mini7_v1)
    expected_body = canonical_rules_dict(expected_snapshot)
    expected_hash = canonical_rules_hash(expected_snapshot)

    async with session_factory() as session:
        game = await games.create(session, ruleset_id=mini7_v1.RULESET_ID, game_seed="snap-game")
        await session.commit()
        game_id = game.id

    async with session_factory() as session:
        loaded = await games.get(session, game_id)
        assert loaded is not None
        assert loaded.rules_snapshot_json == expected_body
        assert loaded.rules_snapshot_hash == expected_hash
        assert FORBIDDEN_SNAPSHOT_KEYS.isdisjoint(_walk_keys(loaded.rules_snapshot_json))


async def test_lobbies_create_persists_canonical_builtin_rules_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    expected_snapshot = snapshot_from_ruleset(mini7_v1)
    expected_body = canonical_rules_dict(expected_snapshot)
    expected_hash = canonical_rules_hash(expected_snapshot)

    async with session_factory() as session:
        principal = await human_principals.create_principal(session, kind="guest")
        league = await leagues.create(
            session, name="humans", ruleset_id=mini7_v1.RULESET_ID, ranked=False
        )
        lobby = await lobbies.create_lobby(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            identity_mode="ANONYMOUS",
            theme_pack_id=None,
            lobby_seed="snap-lobby",
            invite_token="snap-invite",
            integrity_acknowledged=False,
            host_principal_id=principal.id,
            league_id=league.id,
            now=now,
        )
        await session.commit()
        lobby_id = lobby.id

    async with session_factory() as session:
        loaded = await lobbies.get_lobby(session, lobby_id)
        assert loaded is not None
        assert loaded.rules_snapshot_json == expected_body
        assert loaded.rules_snapshot_hash == expected_hash
        assert FORBIDDEN_SNAPSHOT_KEYS.isdisjoint(_walk_keys(loaded.rules_snapshot_json))


async def test_repository_validation_rejects_rules_snapshot_hash_mismatch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    expected_body = canonical_rules_dict(snapshot_from_ruleset(mini7_v1))
    bad_hash = "0" * 64

    async with session_factory() as session:
        with pytest.raises(RulesSnapshotValidationError, match="rules_snapshot_hash_mismatch"):
            await games.create(
                session,
                ruleset_id=mini7_v1.RULESET_ID,
                game_seed="mismatch",
                rules_snapshot_json=expected_body,
                rules_snapshot_hash=bad_hash,
            )


async def test_repository_validation_rejects_lobby_snapshot_mutation_after_launch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    expected_body = canonical_rules_dict(snapshot_from_ruleset(mini7_v1))
    expected_hash = canonical_rules_hash(snapshot_from_ruleset(mini7_v1))

    async with session_factory() as session:
        principal = await human_principals.create_principal(session, kind="guest")
        league = await leagues.create(
            session, name="humans", ruleset_id=mini7_v1.RULESET_ID, ranked=False
        )
        lobby = await lobbies.create_lobby(
            session,
            ruleset_id=mini7_v1.RULESET_ID,
            identity_mode="ANONYMOUS",
            theme_pack_id=None,
            lobby_seed="immutable-lobby",
            invite_token="immutable-invite",
            integrity_acknowledged=False,
            host_principal_id=principal.id,
            league_id=league.id,
            now=now,
        )
        await lobbies.set_lobby_status(
            session, lobby_id=lobby.id, status=LobbyStatus.LAUNCHED.value, now=now
        )
        with pytest.raises(
            RulesSnapshotValidationError, match="rules_snapshot_immutable_after_launch"
        ):
            await lobbies.update_rules_snapshot(
                session,
                lobby_id=lobby.id,
                rules_snapshot_json={**expected_body, "player_count": 9},
                rules_snapshot_hash=expected_hash,
            )


async def test_legacy_null_game_snapshot_still_resolves_registered_ruleset(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        legacy = Game(
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed="legacy-null",
            status="COMPLETED",
            rules_snapshot_json=None,
            rules_snapshot_hash=None,
        )
        session.add(legacy)
        await session.commit()
        game_id = legacy.id

    async with session_factory() as session:
        loaded = await session.get(Game, game_id)
        assert loaded is not None
        assert loaded.rules_snapshot_json is None
        assert loaded.rules_snapshot_hash is None
        legacy_ruleset = get_ruleset(loaded.ruleset_id)

    assert canonical_rules_hash(snapshot_from_ruleset(legacy_ruleset)) == canonical_rules_hash(
        snapshot_from_ruleset(mini7_v1)
    )


def test_lobby_and_game_snapshot_columns_are_nullable_for_legacy_rows() -> None:
    assert Game.__table__.columns["rules_snapshot_json"].nullable is True
    assert Game.__table__.columns["rules_snapshot_hash"].nullable is True
    assert Lobby.__table__.columns["rules_snapshot_json"].nullable is True
    assert Lobby.__table__.columns["rules_snapshot_hash"].nullable is True
