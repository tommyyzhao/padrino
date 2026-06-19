"""End-to-end broadcast pipeline smoke test (US-117).

Exercises the ENTIRE Wave 7 spine in one default-suite test so a cross-boundary
contract break (like the Wave 7 moderation event-type bug, where the gate looked
for the wrong event type and silently saw zero chat) can never again pass a green
suite. The flow under test:

    run_continuous_matchmaking_tick
        -> game COMPLETED
        -> analytics aggregates written
        -> moderation pass marks the game broadcastable
        -> game promoted to LIVE
    consume the full SSE stream (/public/games/{id}/live)
        -> every frame validates against the public_event_v1 golden contract
        -> the terminal GameTerminated frame is last
    mark_recent
        -> recap analytics (/public/games/{id}/analytics) served spoiler-free.

The moderation gate is exercised with a *recording* stub guard whose ``check``
captures the exact messages it was handed; the test asserts at least one
``PublicMessageSubmitted`` text actually reached the gate, regression-guarding
the vacuous-gate class of bug (a gate that gates nothing).

Uses a deterministic chatting mock adapter (no real LLM), a zero cadence (so the
SSE stream is instant), and anonymous public reads.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import RateLimiter
from padrino.api.routes.public import _live_cadence
from padrino.core.agents.coercion import coerce_response_failure
from padrino.core.engine.state import Phase
from padrino.core.enums import PhaseKind
from padrino.core.observations import Observation
from padrino.core.rulesets import mini7_v1
from padrino.db.models import Game, GameEvent
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import prompt_versions as prompt_versions_repo
from padrino.db.repositories import providers as providers_repo
from padrino.llm.adapter import AdapterResult
from padrino.llm.mock import _phase_kind_for
from padrino.llm.prompts import CANONICAL_RESPONSE_SCHEMA, iter_canonical_prompts
from padrino.public.broadcast_index import BroadcastState, mark_recent
from padrino.public.projection import (
    PUBLIC_EVENT_FORBIDDEN_KEYS,
    PUBLIC_EVENT_V1_FIELDS,
)
from padrino.scheduler.continuous_matchmaking import run_continuous_matchmaking_tick
from padrino.settings import Settings, get_settings

_SEATS = [f"P{i + 1:02d}" for i in range(mini7_v1.PLAYER_COUNT)]
_NOW = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Chatting mock adapter: emits a benign public_message during discussion so the
# pipeline produces PublicMessageSubmitted events for the moderation gate.
# ---------------------------------------------------------------------------


class _ChattyMockAdapter:
    """Safe-coercion response, but with a benign public_message in discussion.

    Mirrors :class:`padrino.llm.mock.NoopMockAdapter` (every game runs to the
    MAX_DAYS draw) except that on DAY_DISCUSSION it attaches a benign public
    message, so the game emits ``PublicMessageSubmitted`` events the moderation
    gate must inspect.
    """

    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, observation: Observation) -> AdapterResult:
        self.calls.append((observation.phase, observation.you.player_id))
        phase_kind = _phase_kind_for(observation.phase)
        phase = Phase(kind=phase_kind, day=observation.day, round=observation.round)
        base = coerce_response_failure(phase, "noop")
        if phase_kind is PhaseKind.DAY_DISCUSSION:
            response = base.model_copy(
                update={
                    "public_message": f"Hello from {observation.you.player_id}, I am innocent.",
                }
            )
        else:
            response = base
        return AdapterResult(
            raw_response=response.model_dump_json(),
            parsed_response=response,
            latency_ms=0,
        )


class _RecordingGuard:
    """Always-safe guard that records every batch of messages it was handed."""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    async def check(self, messages: list[str]) -> bool:
        self.seen.append(list(messages))
        return True


# ---------------------------------------------------------------------------
# Seeding (mirrors tests/scheduler/test_continuous_matchmaking.py)
# ---------------------------------------------------------------------------


async def _seed_roster(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session, session.begin():
        canonical: dict[str, Any] = {}
        for template in iter_canonical_prompts(mini7_v1.RULESET_ID):
            canonical[template.role_family.value] = await prompt_versions_repo.create(
                session,
                ruleset_id=template.ruleset_id,
                version=template.version,
                system_prompt=template.system_prompt,
                developer_prompt=template.role_family.value,
                response_schema=CANONICAL_RESPONSE_SCHEMA,
                prompt_hash=template.prompt_hash,
            )
        pv = canonical["VANILLA_TOWN"]
        await leagues_repo.create(
            session,
            name="E2E League",
            ruleset_id=mini7_v1.RULESET_ID,
            ranked=True,
        )
        provider = await providers_repo.create(
            session, name="mockprov", auth_secret_ref="env:MOCK_KEY"
        )
        for seat in _SEATS:
            mc = await model_configs_repo.create(
                session,
                provider_id=provider.id,
                model_name=f"mock/{seat}",
                litellm_model_id=f"mock/{seat}",
                default_temperature=0.7,
                default_top_p=1.0,
                default_max_output_tokens=512,
                supports_structured_outputs=True,
            )
            await agent_builds_repo.create(
                session,
                display_name=f"Mock {seat}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="mock-v1",
                inference_params={},
                active=True,
            )


# ---------------------------------------------------------------------------
# Public client fixtures
# ---------------------------------------------------------------------------


def _zero_cadence() -> Any:
    from padrino.public.broadcaster import CadenceConfig

    return CadenceConfig(chat_ms=0, phase_ms=0, elimination_ms=0, resolution_ms=0, default_ms=0)


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        session_factory=session_factory,
        auth_required=False,
        rate_limiter=RateLimiter(),
    )
    # Anonymous public reads: matches the production public-surface posture.
    app.state.auth_settings = Settings(padrino_public_leaderboard_anonymous=True)
    app.dependency_overrides[_live_cadence] = _zero_cadence
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _parse_sse(body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in body.strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        frame: dict[str, Any] = {}
        for line in block.split("\n"):
            if line.startswith("id:"):
                frame["id"] = int(line[3:].strip())
            elif line.startswith("data:"):
                frame["data"] = json.loads(line[5:].strip())
        if "data" in frame:
            frames.append(frame)
    return frames


def _has_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            k in PUBLIC_EVENT_FORBIDDEN_KEYS or _has_forbidden_key(v) for k, v in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_has_forbidden_key(item) for item in value)
    return False


# ---------------------------------------------------------------------------
# The end-to-end test
# ---------------------------------------------------------------------------


async def test_broadcast_pipeline_end_to_end(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Matchmaking tick -> COMPLETED -> gated -> LIVE -> SSE -> RECENT -> recap."""
    await _seed_roster(session_factory)

    guard = _RecordingGuard()
    settings = Settings(padrino_enable_continuous_matchmaking=True)

    # 1. Matchmaking tick runs one game end-to-end and promotes it to LIVE.
    ran = await run_continuous_matchmaking_tick(
        session_factory,
        settings=settings,
        now=_NOW,
        guard=guard,
        adapter_factory=lambda _builds: _ChattyMockAdapter(),
    )
    assert ran is True

    async with session_factory() as session:
        games = list((await session.execute(select(Game))).scalars())
    assert len(games) == 1
    game = games[0]
    assert game.status == "COMPLETED"
    assert game.is_broadcastable is True
    assert game.broadcast_state == BroadcastState.LIVE.value
    game_id = game.id

    # 2. The moderation gate actually saw real public chat (regression guard
    #    against the vacuous-gate class of bug).
    all_seen = [msg for batch in guard.seen for msg in batch]
    assert all_seen, "moderation gate was never handed any messages"

    async with session_factory() as session:
        pub_msgs = list(
            (
                await session.execute(
                    select(GameEvent).where(
                        GameEvent.game_id == game_id,
                        GameEvent.event_type == "PublicMessageSubmitted",
                    )
                )
            ).scalars()
        )
    assert pub_msgs, "the chatty adapter must have produced PublicMessageSubmitted events"
    emitted_texts = {ev.payload["text"] for ev in pub_msgs}
    assert emitted_texts & set(all_seen), (
        "at least one PublicMessageSubmitted text must have reached the moderation gate"
    )

    # 3. Analytics aggregates were materialized for the seated agents.
    from padrino.db.models import AnalyticsAggregate

    async with session_factory() as session:
        aggregates = list((await session.execute(select(AnalyticsAggregate))).scalars())
    assert aggregates, "analytics aggregates must be written for the completed game"

    # 4. Game is LIVE on the public live index.
    live = await client.get("/public/live")
    assert live.status_code == 200
    live_ids = {item["game_id"] for item in live.json()["items"]}
    assert str(game_id) in live_ids

    # 5. Consume the FULL SSE stream; validate every frame against the
    #    public_event_v1 golden contract and assert the terminal frame is last.
    stream = await client.get(f"/public/games/{game_id}/live")
    assert stream.status_code == 200
    assert "text/event-stream" in stream.headers["content-type"]
    frames = _parse_sse(stream.text)
    assert frames, "the SSE stream produced no frames"

    seqs = [f["id"] for f in frames]
    assert seqs == sorted(seqs), "frames must arrive in sequence order"
    for f in frames:
        data = f["data"]
        assert set(data.keys()) == PUBLIC_EVENT_V1_FIELDS, (
            f"frame violates public_event_v1 field set: {set(data.keys())}"
        )
        assert data["schema_version"] == "public_event_v1"
        assert data["visibility"] == "PUBLIC", "SSE must drop PRIVATE/SYSTEM frames"
        assert f["id"] == data["sequence"], "SSE id must equal the event sequence"
        assert not _has_forbidden_key(data["payload"]), (
            f"forbidden key leaked into a broadcast frame: {data['payload']}"
        )

    assert frames[-1]["data"]["event_type"] == "GameTerminated", (
        "the terminal GameTerminated frame must be the last frame of the stream"
    )
    # At least one chat frame must be on the wire (the gate is not vacuous).
    assert any(f["data"]["event_type"] == "PublicMessageSubmitted" for f in frames)

    # 6. While LIVE, recap analytics are spoiler-safe (winner withheld).
    live_analytics = await client.get(f"/public/games/{game_id}/analytics")
    assert live_analytics.status_code == 200
    assert live_analytics.json()["winner"] is None
    assert live_analytics.headers["cache-control"] == "no-store"

    # 7. Broadcast completes: mark_recent flips the game to RECENT.
    async with session_factory() as session, session.begin():
        result = await mark_recent(session, game_id)
        assert result is not None
        assert result.broadcast_state == BroadcastState.RECENT.value

    # 8. Recent index now carries the game.
    recent = await client.get("/public/recent")
    assert recent.status_code == 200
    recent_ids = {item["game_id"] for item in recent.json()["items"]}
    assert str(game_id) in recent_ids

    # 9. Recap analytics for the RECENT game are served (outcome now visible)
    #    with an immutable CDN cache header. The recap is spoiler-free in the
    #    sense that it never ties a hidden role to a *specific* player: claims
    #    carry only self-declared claimed roles, and role_win_rates/survival are
    #    aggregate-by-role with no player_id->role linkage.
    recap = await client.get(f"/public/games/{game_id}/analytics")
    assert recap.status_code == 200
    body = recap.json()
    assert recap.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert body["winner"] is not None, "RECENT recap must expose the settled outcome"
    for claim in body["claims"]:
        assert set(claim.keys()) == {"player_id", "claimed_role", "sequence", "phase"}, (
            f"recap claim record exposes an unexpected (spoiler) field: {claim}"
        )
