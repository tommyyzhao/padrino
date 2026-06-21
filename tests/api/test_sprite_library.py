"""Tests for the static themed sprite library (US-152).

Covers the asset route (``/public/sprites/*``) and the pure resolution rules in
:mod:`padrino.core.sprites`:

* the manifest serves with an immutable cache header and lists 3-5 theme packs,
* a sprite file serves with an immutable cache header and the SVG media type,
* an unknown theme pack OR an unknown key for a known pack 404s,
* in ANONYMOUS mode every seat maps to a role-AGNOSTIC archetype (no role / no
  human-vs-AI leak), while TRANSPARENT may surface a role sprite,
* unknown / None inputs fall back deterministically (placeholder / archetype).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.core.enums import Role
from padrino.core.sprites import (
    PLACEHOLDER_SPRITE,
    THEME_PACKS,
    build_manifest,
    resolve_seat_sprite,
    sprite_keys_for_pack,
    theme_pack_ids,
)

_IMMUTABLE = "public, max-age=31536000, immutable"


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    # auth_required=True proves the asset route is reachable unauthenticated.
    app = create_app(session_factory=session_factory, auth_required=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def test_library_ships_three_to_five_theme_packs() -> None:
    assert 3 <= len(THEME_PACKS) <= 5


# --- manifest --------------------------------------------------------------


async def test_manifest_serves_with_immutable_cache(client: AsyncClient) -> None:
    resp = await client.get("/public/sprites/manifest")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == _IMMUTABLE
    body = resp.json()
    assert body == build_manifest()
    assert [p["id"] for p in body["theme_packs"]] == list(theme_pack_ids())
    assert body["placeholder"] == PLACEHOLDER_SPRITE


# --- sprite files ----------------------------------------------------------


async def test_archetype_sprite_serves_with_immutable_cache(client: AsyncClient) -> None:
    pack = THEME_PACKS[0]
    key = pack["archetypes"][0]
    resp = await client.get(f"/public/sprites/{pack['id']}/{key}")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == _IMMUTABLE
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert resp.content  # non-empty asset bytes


async def test_placeholder_serves(client: AsyncClient) -> None:
    resp = await client.get("/public/sprites/placeholder")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == _IMMUTABLE
    assert resp.content


async def test_unknown_theme_pack_404s(client: AsyncClient) -> None:
    resp = await client.get("/public/sprites/no_such_pack/archetype_a")
    assert resp.status_code == 404


async def test_unknown_key_for_known_pack_404s(client: AsyncClient) -> None:
    pack = THEME_PACKS[0]
    resp = await client.get(f"/public/sprites/{pack['id']}/no_such_key")
    assert resp.status_code == 404


async def test_every_manifest_key_is_served(client: AsyncClient) -> None:
    # Guards against a manifest key that has no backing static file.
    for pack in THEME_PACKS:
        for key in sorted(sprite_keys_for_pack(pack["id"])):
            resp = await client.get(f"/public/sprites/{pack['id']}/{key}")
            assert resp.status_code == 200, (pack["id"], key)


# --- anonymity-safe resolution --------------------------------------------


def test_anonymous_maps_every_seat_to_role_agnostic_archetype() -> None:
    pack_id = THEME_PACKS[0]["id"]
    archetypes = set(THEME_PACKS[0]["archetypes"])
    role_keys = set(THEME_PACKS[0]["role_sprites"].values())
    for seat in range(10):
        for role in Role:
            sprite = resolve_seat_sprite(
                theme_pack_id=pack_id,
                public_player_id=f"P{seat}",
                role=role,
                identity_mode="ANONYMOUS",
            )
            assert sprite in archetypes
            assert sprite not in role_keys


def test_anonymous_sprite_is_independent_of_role() -> None:
    # A seat's anonymous sprite is keyed ONLY by its id, never its role, so the
    # sprite cannot encode the role.
    pack_id = THEME_PACKS[0]["id"]
    as_detective = resolve_seat_sprite(
        theme_pack_id=pack_id,
        public_player_id="P3",
        role=Role.DETECTIVE,
        identity_mode="ANONYMOUS",
    )
    as_mafia = resolve_seat_sprite(
        theme_pack_id=pack_id,
        public_player_id="P3",
        role=Role.MAFIA_GOON,
        identity_mode="ANONYMOUS",
    )
    assert as_detective == as_mafia


def test_none_mode_coerces_to_anonymous_archetype() -> None:
    pack_id = THEME_PACKS[0]["id"]
    archetypes = set(THEME_PACKS[0]["archetypes"])
    sprite = resolve_seat_sprite(
        theme_pack_id=pack_id,
        public_player_id="P1",
        role=Role.DETECTIVE,
        identity_mode=None,
    )
    assert sprite in archetypes


def test_transparent_surfaces_role_sprite() -> None:
    pack = THEME_PACKS[0]
    sprite = resolve_seat_sprite(
        theme_pack_id=pack["id"],
        public_player_id="P1",
        role=Role.DETECTIVE,
        identity_mode="TRANSPARENT",
    )
    assert sprite == pack["role_sprites"][Role.DETECTIVE.value]


def test_unknown_theme_pack_falls_back_to_placeholder() -> None:
    sprite = resolve_seat_sprite(
        theme_pack_id="no_such_pack",
        public_player_id="P1",
        role=Role.DETECTIVE,
        identity_mode="TRANSPARENT",
    )
    assert sprite == PLACEHOLDER_SPRITE
