"""Static themed sprite library (Wave 9, US-152).

Seats are decorated with themed sprites drawn from a curated, *pre-generated*
static library — there is NO runtime image generation in v1 (the authoring
step that produced the assets is offline / human, see PREP-v9). This module is
the single pure source of truth for:

* the curated set of theme packs (3-5) and the sprite keys each ships,
* the role-agnostic *archetype* sprite every seat maps to in ANONYMOUS mode,
* the deterministic resolution of a seat -> sprite key for a given identity
  mode, with a deterministic placeholder fallback for unknown inputs.

The anonymity invariant (hard rule 7) lives here: in ANONYMOUS mode the sprite
a seat shows is chosen from a per-theme pool of *role-agnostic archetypes* keyed
ONLY by a stable seat token (the public player id). It can never encode the
seat's role nor whether the seat is human or AI. Only in TRANSPARENT mode may
the resolver fall through to a role-specific sprite.

This module is pure core: no IO, no clock, no ``random`` — archetype selection
is a stable hash of the public player id (deterministic placeholder fallback
included). The impure asset route (``padrino.api.routes.sprites``) serves the
manifest and the static files this module describes.
"""

from __future__ import annotations

import hashlib
from typing import TypedDict

from padrino.core.enums import Role
from padrino.core.observation_privacy import is_anonymous


class SpriteThemePack(TypedDict):
    """One curated theme pack in the static library.

    ``id`` is the stable ``theme_pack_id`` a lobby pins (``db.Lobby``);
    ``archetypes`` is the ordered pool of role-AGNOSTIC sprite keys used in
    ANONYMOUS mode; ``role_sprites`` maps a :class:`Role` value to its sprite
    key, used ONLY in TRANSPARENT mode.
    """

    id: str
    display_name: str
    archetypes: list[str]
    role_sprites: dict[str, str]


class SpriteManifest(TypedDict):
    """The full sprite-library manifest served read-only to clients."""

    version: str
    placeholder: str
    theme_packs: list[SpriteThemePack]


#: Manifest schema version — bump when the asset set or shape changes so clients
#: can cache-bust the immutable assets.
MANIFEST_VERSION = "1"

#: The deterministic placeholder sprite, returned for any unknown theme pack or
#: unresolvable seat so a client always has a renderable key (never ``None``).
PLACEHOLDER_SPRITE = "placeholder"

#: The role-agnostic archetypes every theme pack ships, in a stable order. The
#: pool is deliberately disjoint from any role concept so an ANONYMOUS sprite
#: leaks nothing about a seat's role or human/AI nature.
_ARCHETYPE_KEYS: tuple[str, ...] = (
    "archetype_a",
    "archetype_b",
    "archetype_c",
    "archetype_d",
)


def _theme_pack(pack_id: str, display_name: str) -> SpriteThemePack:
    """Build a theme pack with the standard archetype pool + role sprites.

    Every pack ships the SAME set of role-agnostic archetypes and one sprite per
    :class:`Role`; the sprite *keys* are namespaced by ``pack_id`` so a client
    fetches ``/<pack_id>/<key>`` from the asset route. Keeping the shape uniform
    means ANONYMOUS resolution is identical across themes (only the art differs).
    """
    return SpriteThemePack(
        id=pack_id,
        display_name=display_name,
        archetypes=list(_ARCHETYPE_KEYS),
        role_sprites={role.value: f"role_{role.value.lower()}" for role in Role},
    )


#: The curated static library: 3-5 theme packs. The art is shipped as static
#: files under ``padrino/assets/sprites/<theme_pack_id>/`` (see PREP-v9).
THEME_PACKS: tuple[SpriteThemePack, ...] = (
    _theme_pack("classic_noir", "Classic Noir"),
    _theme_pack("pixel_town", "Pixel Town"),
    _theme_pack("woodland", "Woodland Critters"),
    _theme_pack("neon_city", "Neon City"),
)

_THEME_PACKS_BY_ID: dict[str, SpriteThemePack] = {pack["id"]: pack for pack in THEME_PACKS}


def theme_pack_ids() -> tuple[str, ...]:
    """Return the curated theme pack ids in their stable manifest order."""
    return tuple(pack["id"] for pack in THEME_PACKS)


def get_theme_pack(theme_pack_id: str | None) -> SpriteThemePack | None:
    """Return the theme pack for ``theme_pack_id`` or ``None`` if unknown."""
    if theme_pack_id is None:
        return None
    return _THEME_PACKS_BY_ID.get(theme_pack_id)


def build_manifest() -> SpriteManifest:
    """Build the full, read-only sprite-library manifest.

    Pure and deterministic: identical every call so the asset route can serve it
    with an immutable cache header.
    """
    return SpriteManifest(
        version=MANIFEST_VERSION,
        placeholder=PLACEHOLDER_SPRITE,
        theme_packs=list(THEME_PACKS),
    )


def sprite_keys_for_pack(theme_pack_id: str) -> frozenset[str]:
    """Return every valid sprite key for a theme pack (archetypes + roles).

    Used by the asset route to 404 an unknown key. Returns an empty set for an
    unknown theme pack.
    """
    pack = _THEME_PACKS_BY_ID.get(theme_pack_id)
    if pack is None:
        return frozenset()
    return frozenset(pack["archetypes"]) | frozenset(pack["role_sprites"].values())


def _archetype_for_token(archetypes: list[str], token: str) -> str:
    """Pick a role-agnostic archetype for ``token`` by a stable hash.

    Deterministic (sha256 of the token, mod pool size) so the same seat always
    shows the same archetype within a game and across replays, with NO ``random``
    and NO dependence on role or seat kind.
    """
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], "big") % len(archetypes)
    return archetypes[index]


def resolve_seat_sprite(
    *,
    theme_pack_id: str | None,
    public_player_id: str,
    role: Role | str | None,
    identity_mode: object,
) -> str:
    """Resolve the sprite key a seat shows, honouring the identity mode.

    Args:
        theme_pack_id: The lobby's pinned theme pack. Unknown / ``None`` yields
            the deterministic :data:`PLACEHOLDER_SPRITE`.
        public_player_id: The seat's stable, identity-blind token (drives the
            ANONYMOUS archetype choice).
        role: The seat's role. CONSULTED ONLY in TRANSPARENT mode; ignored in
            ANONYMOUS mode so the sprite cannot encode the role.
        identity_mode: The game's identity mode. Coerced FAIL-CLOSED to
            ANONYMOUS by :func:`padrino.core.observation_privacy.is_anonymous`,
            so a missing / ``None`` / unknown mode yields a role-agnostic
            archetype.

    Returns:
        A sprite key valid for the theme pack, or :data:`PLACEHOLDER_SPRITE`.

    In ANONYMOUS mode (the default and fail-closed value) the result is ALWAYS a
    role-agnostic archetype keyed only by ``public_player_id`` — it never depends
    on ``role`` or any human/AI marker.
    """
    pack = get_theme_pack(theme_pack_id)
    if pack is None:
        return PLACEHOLDER_SPRITE

    if is_anonymous(identity_mode):
        return _archetype_for_token(pack["archetypes"], public_player_id)

    # TRANSPARENT mode may surface a role-specific sprite. A missing / unknown
    # role still falls back to the role-agnostic archetype (never a wrong role).
    if role is None:
        return _archetype_for_token(pack["archetypes"], public_player_id)
    role_value = role.value if isinstance(role, Role) else str(role)
    return pack["role_sprites"].get(
        role_value, _archetype_for_token(pack["archetypes"], public_player_id)
    )


__all__ = [
    "MANIFEST_VERSION",
    "PLACEHOLDER_SPRITE",
    "THEME_PACKS",
    "SpriteManifest",
    "SpriteThemePack",
    "build_manifest",
    "get_theme_pack",
    "resolve_seat_sprite",
    "sprite_keys_for_pack",
    "theme_pack_ids",
]
