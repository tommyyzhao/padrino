"""US-063: public read-only API + federated leaderboard tests.

These tests bypass the full game runner — bundles are hand-crafted dicts
inserted directly into ``ingested_games`` so the suite stays fast and the
focus stays on the routing / aggregation / privacy contracts.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import (
    SCOPE_ADMIN,
    SCOPE_SPECTATOR,
    SCOPE_SUBMITTER,
    RateLimiter,
    generate_raw_key,
)
from padrino.api.routes.public import PUBLIC_TRANSCRIPT_FORBIDDEN_KEYS
from padrino.core.enums import RatingContextKind
from padrino.db.models import AgentBuild, PlacementRating, Rating, SoloRateRating
from padrino.db.repositories import (
    agent_builds,
    leagues,
    model_configs,
    prompt_versions,
    providers,
)
from padrino.db.repositories import (
    api_keys as api_keys_repo,
)
from padrino.db.repositories import (
    ingested_games as ingested_games_repo,
)
from padrino.db.repositories import rating_contexts as rating_contexts_repo
from padrino.ratings.public_leaderboard import RATING_MODEL, reset_cache
from padrino.settings import get_settings

_RULESET = "mini7_v1"


@pytest.fixture(autouse=True)
def _reset_cache_and_settings() -> None:
    reset_cache()
    get_settings.cache_clear()


def _make_bundle(
    *,
    game_id: str,
    winner: str,
    gauntlet_id: str | None = None,
    agent_builds: list[dict[str, Any]] | None = None,
    extra_events: list[dict[str, Any]] | None = None,
    tip_hash: str | None = None,
) -> dict[str, Any]:
    """Hand-craft a GameBundle-shaped dict with 2 mafia + 5 town seats."""
    seats = [
        {
            "public_player_id": "P01",
            "seat_index": 0,
            "role": "MAFIOSO",
            "faction": "MAFIA",
            "alive": True,
            "death_phase": None,
        },
        {
            "public_player_id": "P02",
            "seat_index": 1,
            "role": "MAFIOSO",
            "faction": "MAFIA",
            "alive": True,
            "death_phase": None,
        },
        {
            "public_player_id": "P03",
            "seat_index": 2,
            "role": "VILLAGER",
            "faction": "TOWN",
            "alive": True,
            "death_phase": None,
        },
        {
            "public_player_id": "P04",
            "seat_index": 3,
            "role": "VILLAGER",
            "faction": "TOWN",
            "alive": True,
            "death_phase": None,
        },
        {
            "public_player_id": "P05",
            "seat_index": 4,
            "role": "VILLAGER",
            "faction": "TOWN",
            "alive": True,
            "death_phase": None,
        },
        {
            "public_player_id": "P06",
            "seat_index": 5,
            "role": "DOCTOR",
            "faction": "TOWN",
            "alive": True,
            "death_phase": None,
        },
        {
            "public_player_id": "P07",
            "seat_index": 6,
            "role": "DETECTIVE",
            "faction": "TOWN",
            "alive": True,
            "death_phase": None,
        },
    ]
    if agent_builds is None:
        agent_builds = [
            {
                "public_player_id": seat["public_player_id"],
                "seat_index": seat["seat_index"],
                "display_name": "modelA" if seat["faction"] == "TOWN" else "modelB",
                "prompt_version": "v1",
                "model_provider": "providerX",
                "model_name": "modelA" if seat["faction"] == "TOWN" else "modelB",
                "model_version": "1.0",
            }
            for seat in seats
        ]

    events = [
        {
            "sequence": 1,
            "event_type": "RolesAssigned",
            "phase": "SETUP",
            "visibility": "PRIVATE",
            "actor_player_id": None,
            "payload": {
                "assignments": [
                    {
                        "public_player_id": s["public_player_id"],
                        "role": s["role"],
                        "faction": s["faction"],
                    }
                    for s in seats
                ]
            },
            "prev_event_hash": "0" * 64,
            "event_hash": "a" * 64,
        },
        {
            "sequence": 2,
            "event_type": "PublicMessageSubmitted",
            "phase": "DAY_1_DISCUSSION_ROUND_1",
            "visibility": "PUBLIC",
            "actor_player_id": "P03",
            "payload": {"text": "hello world", "round_index": 1},
            "prev_event_hash": "a" * 64,
            "event_hash": "b" * 64,
        },
    ]
    if extra_events:
        events.extend(extra_events)

    return {
        "schema_version": "padrino.export.v1",
        "ruleset_id": _RULESET,
        "league_id": None,
        "gauntlet_id": gauntlet_id,
        "game_id": game_id,
        "seed": "seed-" + game_id,
        "terminal_result": {"winner": winner, "reason": "TOWN_VOTE", "day_terminated": 2},
        "tip_hash": tip_hash or ("c" * 64),
        "agent_builds": agent_builds,
        "game_seats": seats,
        "events": events,
        "signer_fingerprint": None,
        "sig": None,
    }


async def _insert_ingested(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    bundle: dict[str, Any],
    submitter_key_id: uuid.UUID | None = None,
    signer_fingerprint: str | None = None,
    verification_status: str = "verified",
) -> None:
    async with session_factory() as session, session.begin():
        await ingested_games_repo.create(
            session,
            game_id=str(bundle["game_id"]),
            ruleset_id=str(bundle["ruleset_id"]),
            league_id=bundle.get("league_id"),
            gauntlet_id=bundle.get("gauntlet_id"),
            tip_hash=str(bundle["tip_hash"]),
            signer_fingerprint=signer_fingerprint,
            verification_status=verification_status,
            submitter_key_id=submitter_key_id,
            bundle=bundle,
        )


async def _seed_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scopes: list[str],
    label: str,
    submission_public_key: str | None = None,
) -> tuple[str, uuid.UUID]:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        obj = await api_keys_repo.create(
            session,
            raw_key=raw,
            scopes=scopes,
            label=label,
            submission_public_key=submission_public_key,
        )
        return raw, obj.id


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def _seed_context_card_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seed canonical, placement, and solo-rate rows for the card contract."""
    async with session_factory() as session, session.begin():
        provider = await providers.create(
            session,
            name="cerebras",
            auth_secret_ref="CEREBRAS_API_KEY",
        )
        mc = await model_configs.create(
            session,
            provider_id=provider.id,
            model_name="glm-4.7",
            model_version="2026-06",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )

        async def build_for(ruleset_id: str, suffix: str) -> AgentBuild:
            pv = await prompt_versions.create(
                session,
                ruleset_id=ruleset_id,
                version=f"{ruleset_id}-{suffix}",
                system_prompt="sys",
                developer_prompt="dev",
                response_schema={"type": "object"},
                prompt_hash=f"context-card-{ruleset_id}-{suffix}",
            )
            return await agent_builds.create(
                session,
                display_name=f"Atlas {suffix}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="2026.05",
                inference_params={"temperature": 0.7},
                active=True,
            )

        canonical_ranked = await build_for("mini7_v1", "ranked")
        canonical_provisional = await build_for("mini7_v1", "provisional")
        placement_build = await build_for("sk12_v1", "placement")
        solo_ranked = await build_for("jester8_v1", "solo-ranked")
        solo_provisional = await build_for("jester8_v1", "solo-provisional")

        canonical_league = await leagues.create(
            session,
            name="context-card-canonical",
            ruleset_id="mini7_v1",
            ranked=True,
        )
        canonical_context = await rating_contexts_repo.get_by_ruleset_kind(
            session,
            ruleset_id="mini7_v1",
            kind=RatingContextKind.CANONICAL_TEAM,
        )
        assert canonical_context is not None
        session.add_all(
            [
                Rating(
                    league_id=canonical_league.id,
                    ruleset_id="mini7_v1",
                    rating_context_id=canonical_context.id,
                    agent_build_id=canonical_ranked.id,
                    scope_type="GLOBAL",
                    scope_value="global",
                    mu=31.2,
                    sigma=2.1,
                    conservative_score=24.9,
                    games=12,
                ),
                Rating(
                    league_id=canonical_league.id,
                    ruleset_id="mini7_v1",
                    rating_context_id=canonical_context.id,
                    agent_build_id=canonical_provisional.id,
                    scope_type="GLOBAL",
                    scope_value="global",
                    mu=50.0,
                    sigma=1.0,
                    conservative_score=47.0,
                    games=2,
                ),
            ]
        )

        placement_context = await rating_contexts_repo.ensure_declared_context(
            session,
            ruleset_id="sk12_v1",
        )
        assert placement_context is not None
        session.add(
            PlacementRating(
                rating_context_id=placement_context.id,
                agent_build_id=placement_build.id,
                scope_type="GLOBAL",
                scope_value="global",
                mu=24.8,
                sigma=4.9,
                conservative_score=10.1,
                games=14,
            )
        )

        solo_context = await rating_contexts_repo.ensure_declared_context(
            session,
            ruleset_id="jester8_v1",
        )
        assert solo_context is not None
        session.add_all(
            [
                SoloRateRating(
                    rating_context_id=solo_context.id,
                    agent_build_id=solo_ranked.id,
                    scope_type="ROLE",
                    scope_value="JESTER",
                    successes=7,
                    attempts=12,
                    posterior_alpha=8.0,
                    posterior_beta=6.0,
                    mean_success_rate=8.0 / 14.0,
                ),
                SoloRateRating(
                    rating_context_id=solo_context.id,
                    agent_build_id=solo_provisional.id,
                    scope_type="ROLE",
                    scope_value="JESTER",
                    successes=8,
                    attempts=9,
                    posterior_alpha=9.0,
                    posterior_beta=2.0,
                    mean_success_rate=9.0 / 11.0,
                ),
            ]
        )


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


@pytest_asyncio.fixture
async def anonymous_client(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("PADRINO_PUBLIC_LEADERBOARD_ANONYMOUS", "true")
    get_settings.cache_clear()
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def test_leaderboard_returns_separated_context_cards_without_cross_sort(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="lurker")
    await _seed_context_card_rows(session_factory)

    response = await client.get("/public/leaderboard", headers=_auth(raw))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ruleset_id"] is None
    assert body["entries"] == []
    assert set(body) >= {"canonical_cards", "experimental_cards"}

    canonical = body["canonical_cards"]
    experimental = body["experimental_cards"]
    assert canonical
    assert experimental
    assert {card["section"] for card in canonical} == {"canonical"}
    assert {card["section_label"] for card in canonical} == {"Ranked canonical"}
    assert {card["context_kind"] for card in canonical} == {"CANONICAL_TEAM"}
    assert {card["section"] for card in experimental} == {"experimental"}
    assert {card["section_label"] for card in experimental} == {"Experimental context"}
    assert {card["context_kind"] for card in experimental} == {"PLACEMENT", "SOLO_RATE"}

    canonical_ranked = next(card for card in canonical if card["display_name"] == "Atlas ranked")
    canonical_provisional = next(
        card for card in canonical if card["display_name"] == "Atlas provisional"
    )
    assert canonical_ranked["rank"] == 1
    assert canonical_ranked["provisional"] is False
    assert canonical_provisional["rank"] is None
    assert canonical_provisional["provisional"] is True
    assert canonical_provisional["conservative_score"] > canonical_ranked["conservative_score"]
    assert "10 games" in canonical_provisional["provisional_reason"]

    placement = next(card for card in experimental if card["context_kind"] == "PLACEMENT")
    assert placement["context_label"] == "Serial Killer 12 placement"
    assert placement["rank"] == 1
    assert placement["metric"] == "openskill_conservative"
    assert placement["conservative_score"] == pytest.approx(10.1)

    solo_ranked = next(card for card in experimental if card["display_name"] == "Atlas solo-ranked")
    solo_provisional = next(
        card for card in experimental if card["display_name"] == "Atlas solo-provisional"
    )
    assert solo_ranked["context_label"] == "Jester 8 lynch-bait"
    assert solo_ranked["rank"] == 1
    assert solo_ranked["metric"] == "solo_success_rate"
    assert solo_ranked["credible_interval_low"] < solo_ranked["mean_success_rate"]
    assert solo_ranked["credible_interval_high"] > solo_ranked["mean_success_rate"]
    assert solo_provisional["rank"] is None
    assert solo_provisional["provisional"] is True
    assert solo_provisional["sample_count"] == 9
    assert "10 attempts" in solo_provisional["provisional_reason"]


async def test_leaderboard_sorted_by_conservative_score(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="lurker")
    # Two town wins: modelA (town) gains rating, modelB (mafia) loses.
    for i in range(3):
        await _insert_ingested(
            session_factory,
            bundle=_make_bundle(game_id=f"g-town-{i}", winner="TOWN"),
        )

    response = await client.get(
        "/public/leaderboard",
        params={"ruleset_id": _RULESET},
        headers=_auth(raw),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rating_model"] == RATING_MODEL
    assert body["ruleset_id"] == _RULESET
    scores = [entry["conservative_score"] for entry in body["entries"]]
    assert scores == sorted(scores, reverse=True)
    # modelA (town) should rank above modelB (mafia) after 3 town wins.
    names = [entry["display_name"] for entry in body["entries"]]
    assert names[0] == "modelA"
    assert names[-1] == "modelB"


async def test_leaderboard_cursor_pagination_stable(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="lurker")
    # Five distinct display names → at least 5 entries in the leaderboard.
    for idx in range(5):
        builds = [
            {
                "public_player_id": f"P{i + 1:02d}",
                "seat_index": i,
                "display_name": f"build-{idx}-{i}",
                "prompt_version": "v1",
                "model_provider": "providerX",
                "model_name": "m",
                "model_version": "1.0",
            }
            for i in range(7)
        ]
        await _insert_ingested(
            session_factory,
            bundle=_make_bundle(game_id=f"g-pager-{idx}", winner="TOWN", agent_builds=builds),
        )

    first = (
        await client.get(
            "/public/leaderboard",
            params={"ruleset_id": _RULESET, "limit": 3},
            headers=_auth(raw),
        )
    ).json()
    assert len(first["entries"]) == 3
    assert first["next_cursor"] is not None
    second = (
        await client.get(
            "/public/leaderboard",
            params={
                "ruleset_id": _RULESET,
                "limit": 3,
                "cursor": first["next_cursor"],
            },
            headers=_auth(raw),
        )
    ).json()
    # Pages don't overlap.
    first_ids = {e["entity_id"] for e in first["entries"]}
    second_ids = {e["entity_id"] for e in second["entries"]}
    assert first_ids.isdisjoint(second_ids)


async def test_leaderboard_invalid_cursor_400(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="lurker")
    response = await client.get(
        "/public/leaderboard",
        params={"ruleset_id": _RULESET, "cursor": "not-a-real-cursor"},
        headers=_auth(raw),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_cursor"


async def test_leaderboard_gauntlet_filter_isolated(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="lurker")
    await _insert_ingested(
        session_factory,
        bundle=_make_bundle(game_id="g-a", winner="TOWN", gauntlet_id="bracket-A"),
    )
    await _insert_ingested(
        session_factory,
        bundle=_make_bundle(game_id="g-b", winner="MAFIA", gauntlet_id="bracket-B"),
    )

    a_body = (
        await client.get(
            "/public/leaderboard",
            params={"ruleset_id": _RULESET, "gauntlet_id": "bracket-A"},
            headers=_auth(raw),
        )
    ).json()
    b_body = (
        await client.get(
            "/public/leaderboard",
            params={"ruleset_id": _RULESET, "gauntlet_id": "bracket-B"},
            headers=_auth(raw),
        )
    ).json()
    # Town won in A so modelA leads; mafia won in B so modelB leads.
    assert a_body["entries"][0]["display_name"] == "modelA"
    assert b_body["entries"][0]["display_name"] == "modelB"


async def test_anonymous_flag_toggles_auth(
    anonymous_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_ingested(
        session_factory,
        bundle=_make_bundle(game_id="g-anon", winner="TOWN"),
    )
    response = await anonymous_client.get(
        "/public/leaderboard",
        params={"ruleset_id": _RULESET},
    )
    assert response.status_code == 200, response.text


async def test_default_requires_spectator_scope(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # No Bearer header → 401 because auth_required=True and anonymous flag is off.
    response = await client.get(
        "/public/leaderboard",
        params={"ruleset_id": _RULESET},
    )
    assert response.status_code == 401
    # Wrong scope (submitter alone) is 403.
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SUBMITTER], label="sub")
    response = await client.get(
        "/public/leaderboard",
        params={"ruleset_id": _RULESET},
        headers=_auth(raw),
    )
    assert response.status_code == 403


async def test_unknown_query_param_returns_422(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="lurker")
    response = await client.get(
        "/public/leaderboard",
        params={"ruleset_id": _RULESET, "bogus": "yes"},
        headers=_auth(raw),
    )
    assert response.status_code == 422


async def test_public_game_returns_bundle_minus_pii(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="lurker")
    bundle = _make_bundle(game_id="g-detail", winner="TOWN")
    # Inject a fake submitter PII key to confirm it gets scrubbed.
    bundle["submitter_label"] = "leaky"
    bundle["submitter_key_id"] = "leaky"
    await _insert_ingested(session_factory, bundle=bundle)

    response = await client.get("/public/games/g-detail", headers=_auth(raw))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["game_id"] == "g-detail"
    assert "submitter_label" not in body["bundle"]
    assert "submitter_key_id" not in body["bundle"]


async def test_public_game_events_paginates(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="lurker")
    bundle = _make_bundle(game_id="g-events", winner="TOWN")
    await _insert_ingested(session_factory, bundle=bundle)

    response = await client.get(
        "/public/games/g-events/events",
        params={"limit": 1},
        headers=_auth(raw),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_estimate"] == 2
    assert len(body["items"]) == 1
    assert body["next_cursor"] is not None

    next_page = await client.get(
        "/public/games/g-events/events",
        params={"limit": 1, "cursor": body["next_cursor"]},
        headers=_auth(raw),
    )
    assert next_page.status_code == 200
    assert len(next_page.json()["items"]) == 1


async def test_transcript_drops_forbidden_keys(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="lurker")
    extra_events = [
        # A public message carrying a leaked role marker — must be dropped.
        {
            "sequence": 3,
            "event_type": "PublicMessageSubmitted",
            "phase": "DAY_1_DISCUSSION_ROUND_1",
            "visibility": "PUBLIC",
            "actor_player_id": "P04",
            "payload": {"text": "im the doctor", "role": "DOCTOR"},
            "prev_event_hash": "b" * 64,
            "event_hash": "c1" + "c" * 62,
        },
        {
            "sequence": 4,
            "event_type": "PublicMessageSubmitted",
            "phase": "DAY_1_DISCUSSION_ROUND_2",
            "visibility": "PUBLIC",
            "actor_player_id": "P05",
            "payload": {"text": "vote them", "round_index": 2},
            "prev_event_hash": "c1" + "c" * 62,
            "event_hash": "d" * 64,
        },
    ]
    bundle = _make_bundle(game_id="g-transcript", winner="TOWN", extra_events=extra_events)
    # Also inject a forbidden key into the terminal_result to verify it is stripped.
    bundle["terminal_result"]["model_name"] = "leaked"
    await _insert_ingested(session_factory, bundle=bundle)

    response = await client.get(
        "/public/games/g-transcript/transcript",
        headers=_auth(raw),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Two clean public messages survive (P03 default + P05), the role-leak one is gone.
    actors = [entry["actor_player_id"] for entry in body["public_chat"]]
    assert actors == ["P03", "P05"]
    for entry in body["public_chat"]:
        assert "role" not in entry
        assert "faction" not in entry
    assert "model_name" not in (body["outcome"] or {})
    # Sanity: the forbidden_payload_keys list aligns with the engine's guard.
    assert set(body["forbidden_payload_keys"]) == set(PUBLIC_TRANSCRIPT_FORBIDDEN_KEYS)


async def test_transcript_404_when_missing(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="lurker")
    response = await client.get("/public/games/g-nonexistent/transcript", headers=_auth(raw))
    assert response.status_code == 404


async def test_submitters_listing_hides_raw_keys(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw_spec, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="lurker")
    pubkey_b64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    raw_sub, submitter_id = await _seed_key(
        session_factory,
        scopes=[SCOPE_SUBMITTER],
        label="research-lab-A",
        submission_public_key=pubkey_b64,
    )
    await _insert_ingested(
        session_factory,
        bundle=_make_bundle(game_id="g-sub-1", winner="TOWN"),
        submitter_key_id=submitter_id,
    )

    response = await client.get("/public/submitters", headers=_auth(raw_spec))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_estimate"] >= 1
    found = next(item for item in body["items"] if item["label"] == "research-lab-A")
    assert found["game_count"] == 1
    # Fingerprint is sha256(public_key)[:32]; the raw key never appears anywhere.
    expected_fp = hashlib.sha256(base64.urlsafe_b64decode(pubkey_b64.encode("ascii"))).hexdigest()[
        :32
    ]
    assert found["submission_public_key_fingerprint"] == expected_fp
    text = response.text
    assert raw_sub not in text
    assert raw_spec not in text
    assert pubkey_b64 not in text


async def test_submitters_admin_submission_not_listed(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw_admin, _ = await _seed_key(session_factory, scopes=[SCOPE_ADMIN], label="root")
    # Admin-submitted row has submitter_key_id=None — should NOT surface as a submitter.
    await _insert_ingested(
        session_factory,
        bundle=_make_bundle(game_id="g-admin", winner="TOWN"),
        submitter_key_id=None,
    )
    response = await client.get("/public/submitters", headers=_auth(raw_admin))
    assert response.status_code == 200
    labels = [item["label"] for item in response.json()["items"]]
    assert "root" not in labels  # root key never ingested a row keyed to itself


async def test_cache_tag_invalidates_on_new_submission(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="lurker")
    await _insert_ingested(
        session_factory,
        bundle=_make_bundle(game_id="g-cache-1", winner="TOWN"),
    )
    first = (
        await client.get(
            "/public/leaderboard",
            params={"ruleset_id": _RULESET},
            headers=_auth(raw),
        )
    ).json()

    await _insert_ingested(
        session_factory,
        bundle=_make_bundle(game_id="g-cache-2", winner="MAFIA"),
    )
    second = (
        await client.get(
            "/public/leaderboard",
            params={"ruleset_id": _RULESET},
            headers=_auth(raw),
        )
    ).json()

    assert first["cache_tag"] != second["cache_tag"]


async def test_leaderboard_filters_unverified_games(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw, _ = await _seed_key(session_factory, scopes=[SCOPE_SPECTATOR], label="lurker")
    # Insert one verified game and one unverified game
    await _insert_ingested(
        session_factory,
        bundle=_make_bundle(game_id="g-verified", winner="TOWN"),
        verification_status="verified",
    )
    await _insert_ingested(
        session_factory,
        bundle=_make_bundle(game_id="g-unverified", winner="TOWN"),
        verification_status="unverified",
    )

    response = await client.get(
        "/public/leaderboard",
        params={"ruleset_id": _RULESET},
        headers=_auth(raw),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Verified town win: modelA games count should be 5 (from g-verified town seats), NOT 10.
    entries = {entry["display_name"]: entry for entry in body["entries"]}
    assert entries["modelA"]["games"] == 5
    assert entries["modelB"]["games"] == 2


async def test_anonymous_rate_limiting(
    anonymous_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Set the anonymous limit to 2 so the test stays fast.
    monkeypatch.setattr(
        get_settings(),
        "padrino_rate_limit_anonymous_per_minute",
        2,
    )
    # Clear the settings cache
    get_settings.cache_clear()

    # First two requests pass.
    for _ in range(2):
        response = await anonymous_client.get(
            "/public/leaderboard",
            params={"ruleset_id": _RULESET},
        )
        assert response.status_code == 200, response.text

    # Third request is rate limited with 429.
    response = await anonymous_client.get(
        "/public/leaderboard",
        params={"ruleset_id": _RULESET},
    )
    assert response.status_code == 429
    assert response.json()["detail"] == "rate_limited"
    assert "Retry-After" in response.headers
