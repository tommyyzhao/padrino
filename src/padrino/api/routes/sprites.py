"""Static themed sprite library asset route (Wave 9, US-152).

Serves the curated, pre-generated sprite library read-only:

* ``GET /public/sprites/manifest`` returns the :func:`build_manifest` document
  (theme packs, archetype pools, role-sprite keys, placeholder).
* ``GET /public/sprites/{theme_pack_id}/{sprite_key}`` returns one static sprite
  file. An unknown theme pack OR an unknown key for a known pack 404s; the
  validity check goes through :mod:`padrino.core.sprites` so the route can never
  serve a key that is not in the manifest.
* ``GET /public/sprites/placeholder`` returns the deterministic placeholder.

Every asset is served with an immutable ``Cache-Control`` (the bytes never
change for a given key; clients cache-bust via the manifest ``version``). There
is NO runtime image generation — the files are authored offline (PREP-v9) and
read from the bundled package via :mod:`importlib.resources`.

Mounted unauthenticated alongside the public surface: sprites are static art and
carry no identity signal (anonymity is enforced upstream by
:func:`padrino.core.sprites.resolve_seat_sprite`, which only ever hands a client
a role-agnostic archetype key in anonymous mode).
"""

from __future__ import annotations

from importlib import resources
from importlib.resources.abc import Traversable

from fastapi import APIRouter, HTTPException, Path, Response, status

from padrino.core.sprites import (
    PLACEHOLDER_SPRITE,
    SpriteManifest,
    build_manifest,
    get_theme_pack,
    sprite_keys_for_pack,
)

router = APIRouter(prefix="/public/sprites", tags=["sprites"])

#: Assets never change for a given key, so clients may cache forever; a new asset
#: set bumps the manifest ``version`` for cache-busting.
_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"

_SVG_MEDIA_TYPE = "image/svg+xml"

# Path segments are constrained so a request can never traverse outside the
# bundled sprite tree (no ``/`` or ``.`` in a segment).
_SEGMENT_PATTERN = r"^[a-z0-9_]+$"


def _assets_root() -> Traversable:
    return resources.files("padrino.assets").joinpath("sprites")


def _read_sprite_bytes(*parts: str) -> bytes | None:
    """Read a bundled sprite file's bytes, or ``None`` if it is missing.

    ``parts`` are already-validated path segments (``[a-z0-9_]+``); the trailing
    segment is the sprite key without its ``.svg`` suffix.
    """
    node = _assets_root()
    for part in parts[:-1]:
        node = node.joinpath(part)
    node = node.joinpath(f"{parts[-1]}.svg")
    try:
        return node.read_bytes()
    except (FileNotFoundError, OSError):
        return None


@router.get("/manifest")
async def get_sprite_manifest(http_response: Response) -> SpriteManifest:
    """Return the read-only sprite-library manifest with an immutable cache."""
    http_response.headers["Cache-Control"] = _IMMUTABLE_CACHE
    return build_manifest()


@router.get("/placeholder")
async def get_placeholder_sprite() -> Response:
    """Return the deterministic placeholder sprite."""
    data = _read_sprite_bytes(PLACEHOLDER_SPRITE)
    if data is None:  # pragma: no cover - placeholder is always bundled
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sprite_not_found")
    return Response(
        content=data,
        media_type=_SVG_MEDIA_TYPE,
        headers={"Cache-Control": _IMMUTABLE_CACHE},
    )


@router.get("/{theme_pack_id}/{sprite_key}")
async def get_sprite(
    theme_pack_id: str = Path(pattern=_SEGMENT_PATTERN, max_length=64),
    sprite_key: str = Path(pattern=_SEGMENT_PATTERN, max_length=64),
) -> Response:
    """Return one static sprite file for a theme pack.

    404s when the theme pack is unknown or ``sprite_key`` is not a valid key for
    that pack (validated against the manifest, never the filesystem alone).
    """
    if get_theme_pack(theme_pack_id) is None or sprite_key not in sprite_keys_for_pack(
        theme_pack_id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sprite_not_found")

    data = _read_sprite_bytes(theme_pack_id, sprite_key)
    if data is None:  # pragma: no cover - manifest key implies a bundled file
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sprite_not_found")
    return Response(
        content=data,
        media_type=_SVG_MEDIA_TYPE,
        headers={"Cache-Control": _IMMUTABLE_CACHE},
    )


__all__ = ["router"]
