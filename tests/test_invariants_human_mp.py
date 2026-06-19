"""Suite-blocking human-multiplayer invariant gates (Wave 9, US-146).

The two NON-NEGOTIABLE Wave-9 invariants are promoted here to comprehensive,
default-suite gates so no future change can silently regress them:

ANONYMITY
    A completed human game provably leaks ZERO human-vs-AI / model-identity
    markers on ANY pre-reveal surface: every live frame, the public_event_v1
    projection, the spectator projection, the per-seat observation guard, and
    the export bundle. The endgame reveal is the ONLY surface that discloses the
    truth, and only once the game is terminal (RECENT) — a LIVE game 404s.

SEGREGATION
    A completed human-lane game writes ZERO rows to the scientific ``ratings`` /
    ``rating_events`` tables (and zero to the dormant human-rating siblings in
    casual v1).

This module *aggregates* the property scaffolds from US-124
(:mod:`tests.core.test_anonymity_property`) and US-125
(:mod:`tests.ratings.test_segregation`) and exercises them end-to-end against a
real, persisted, mixed human+AI game driven to terminal. The game itself is
driven with the engine-transparent mixed-adapter pattern from US-139 (human
seats resolve their turn from a deterministic buffer under an injected clock),
so the whole gate stays deterministic without touching the wall clock or a real
LLM.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_SPECTATOR, RateLimiter, generate_raw_key
from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, IdentityMode, Role, SeatKind
from padrino.core.observation_privacy import (
    HUMAN_IDENTITY_KEYS,
    IDENTITY_MARKER_KEYS,
    AnonymityViolation,
    assert_anonymous_safe,
    assert_no_identity_markers,
    project_game_row,
    project_seat_row,
)
from padrino.core.observations import Observation
from padrino.core.rulesets import mini7_v1
from padrino.core.spectator_projection import project_events_for_spectator
from padrino.db.models import (
    Game,
    GameSeat,
    HumanRating,
    HumanRatingEvent,
    Rating,
    RatingEvent,
)
from padrino.db.repositories import agent_builds as agent_builds_repo
from padrino.db.repositories import games as games_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import model_configs as model_configs_repo
from padrino.db.repositories import prompt_versions as prompt_versions_repo
from padrino.db.repositories import providers as providers_repo
from padrino.export.bundle import assert_bundle_payload_safe, export_game
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.llm.human_adapter import HumanAdapter
from padrino.llm.mock import DeterministicMockAdapter
from padrino.llm.multiplex import SeatMultiplexAdapter
from padrino.public.broadcast_index import BroadcastState
from padrino.public.live_tail import LiveTailConfig, stream_live_tail
from padrino.public.projection import to_public_events_v1
from padrino.runner.game_runner import GameConfig, GamePersistence, run_game
from padrino.settings import get_settings
from tests.conftest import make_town_win_script

pytestmark = pytest.mark.asyncio

_GAME_SEED = "seed-invariants-human-mp-001"
_RULESET = mini7_v1.RULESET_ID


# --------------------------------------------------------------------------- #
# Mixed human+AI driving (US-139 pattern, deterministic clock)
# --------------------------------------------------------------------------- #


class _FakeClock:
    """Monotonic clock advancing only when the injected ``sleep`` is awaited."""

    def __init__(self) -> None:
        self._now = 0.0

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self._now += seconds


class _ScriptedSeatAdapter:
    """An LLM adapter returning one seat's slice of a phase-keyed script."""

    def __init__(self, seat_id: str, script: Mapping[tuple[str, str], AgentResponse]) -> None:
        self._inner = DeterministicMockAdapter(
            {key: resp for key, resp in script.items() if key[1] == seat_id}
        )

    async def complete(self, observation: Observation) -> AdapterResult:
        return await self._inner.complete(observation)


def _human_adapter(
    seat_id: str,
    script: Mapping[tuple[str, str], AgentResponse],
    clock: _FakeClock,
) -> HumanAdapter:
    async def pull(observation: Observation) -> Action | None:
        return script[(observation.phase, seat_id)].action

    return HumanAdapter(
        pull_action=pull,
        deadline_seconds=30.0,
        poll_interval_seconds=0.5,
        clock=clock.now,
        sleep=clock.sleep,
    )


def _split_factions() -> tuple[list[str], list[str], str, str]:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    mafia = [s.public_player_id for s in seats if s.faction is Faction.MAFIA]
    town = [s.public_player_id for s in seats if s.faction is Faction.TOWN]
    doctor = next(s.public_player_id for s in seats if s.role is Role.DOCTOR)
    detective = next(s.public_player_id for s in seats if s.role is Role.DETECTIVE)
    return mafia, town, doctor, detective


def _mixed_adapter(
    human_seats: set[str],
    script: Mapping[tuple[str, str], AgentResponse],
    clock: _FakeClock,
) -> SeatMultiplexAdapter:
    seats = assign_roles(_GAME_SEED, mini7_v1)
    adapters: dict[str, LlmAdapter] = {}
    for seat in seats:
        sid = seat.public_player_id
        adapters[sid] = (
            _human_adapter(sid, script, clock)
            if sid in human_seats
            else _ScriptedSeatAdapter(sid, script)
        )
    return SeatMultiplexAdapter(adapters)


# --------------------------------------------------------------------------- #
# Human-lane game seeding (humans-included league; some seats human-occupied)
# --------------------------------------------------------------------------- #


async def _seed_human_lane_game(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    human_seat_ids: set[str],
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    """Seed the humans-included league + a LIVE human-lane game.

    Human-occupied seats get NO agent build. Returns
    ``(league_id, game_id, agent_builds_by_ai_seat)``.
    """
    async with session_factory() as session, session.begin():
        provider = await providers_repo.create(
            session, name="cerebras", auth_secret_ref="CEREBRAS_API_KEY"
        )
        mc = await model_configs_repo.create(
            session,
            provider_id=provider.id,
            model_name="zai-glm-4.7",
            default_temperature=0.7,
            default_top_p=1.0,
            default_max_output_tokens=4096,
            supports_structured_outputs=True,
        )
        builds: dict[str, uuid.UUID] = {}
        for i in range(mini7_v1.PLAYER_COUNT):
            seat_id = f"P{i + 1:02d}"
            if seat_id in human_seat_ids:
                continue
            pv = await prompt_versions_repo.create(
                session,
                ruleset_id=_RULESET,
                version=f"v{i + 1}",
                system_prompt="sys",
                developer_prompt="dev",
                response_schema={"type": "object"},
                prompt_hash=f"us146-{i}",
            )
            ab = await agent_builds_repo.create(
                session,
                display_name=f"build-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="2026.05",
                inference_params={"temperature": 0.7},
                active=True,
            )
            builds[seat_id] = ab.id

        league = await leagues_repo.get_or_create_humans_included(session, ruleset_id=_RULESET)
        game = await games_repo.create(
            session,
            ruleset_id=_RULESET,
            game_seed=_GAME_SEED,
            status="RUNNING",
        )
        # Mark the game LIVE + broadcastable so the live-tail / reveal gating is
        # exercised against the real columns.
        game.broadcast_state = BroadcastState.LIVE.value
        game.is_broadcastable = True
        game.identity_mode = IdentityMode.ANONYMOUS.value
        league_id = league.id
        game_id = game.id
    return league_id, game_id, builds


async def _persist_human_seat_rows(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    game_id: uuid.UUID,
    human_seat_ids: set[str],
    takeover_seat_id: str,
    ai_builds: dict[str, uuid.UUID],
) -> None:
    """Persist GameSeat rows carrying every identity column (HUMAN/AI/AI_TAKEOVER).

    The runner only backfills seats when EVERY seat has an agent build, so a
    mixed human game needs its seat rows written explicitly. These rows carry the
    full identity surface (``seat_kind``, ``occupant_principal_id`` via None,
    ``takeover_agent_build_id``) so the column-level anonymity guard is exercised
    against real data.
    """
    seats = assign_roles(_GAME_SEED, mini7_v1)
    async with session_factory() as session, session.begin():
        for seat in seats:
            sid = seat.public_player_id
            if sid == takeover_seat_id:
                kind = SeatKind.AI_TAKEOVER.value
                takeover_build = next(iter(ai_builds.values()))
            elif sid in human_seat_ids:
                kind = SeatKind.HUMAN.value
                takeover_build = None
            else:
                kind = SeatKind.AI.value
                takeover_build = None
            session.add(
                GameSeat(
                    game_id=game_id,
                    public_player_id=sid,
                    seat_index=seat.seat_index,
                    agent_build_id=ai_builds.get(sid),
                    seat_kind=kind,
                    takeover_agent_build_id=takeover_build,
                    taken_over_at_phase="DAY_1_VOTE"
                    if kind == SeatKind.AI_TAKEOVER.value
                    else None,
                    role=seat.role.value,
                    faction=seat.faction.value,
                    alive=True,
                )
            )


async def _count_rating_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int, int, int]:
    async with session_factory() as session:
        ratings = (await session.execute(select(func.count()).select_from(Rating))).scalar_one()
        rating_events = (
            await session.execute(select(func.count()).select_from(RatingEvent))
        ).scalar_one()
        human_ratings = (
            await session.execute(select(func.count()).select_from(HumanRating))
        ).scalar_one()
        human_rating_events = (
            await session.execute(select(func.count()).select_from(HumanRatingEvent))
        ).scalar_one()
    return ratings, rating_events, human_ratings, human_rating_events


# --------------------------------------------------------------------------- #
# Fixture: drive ONE real persisted human-lane game to terminal
# --------------------------------------------------------------------------- #


class _PlayedGame:
    def __init__(
        self,
        *,
        game_id: uuid.UUID,
        league_id: uuid.UUID,
        ai_builds: dict[str, uuid.UUID],
        human_seats: set[str],
        takeover_seat: str,
    ) -> None:
        self.game_id = game_id
        self.league_id = league_id
        self.ai_builds = ai_builds
        self.human_seats = human_seats
        self.takeover_seat = takeover_seat


@pytest_asyncio.fixture
async def played_human_game(
    session_factory: async_sessionmaker[AsyncSession],
) -> _PlayedGame:
    """Drive a real mixed human+AI game to terminal on the humans-included league."""
    mafia, town, doctor, detective = _split_factions()
    script = make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )
    human_seats = {town[0], mafia[0]}
    takeover_seat = town[1]

    league_id, game_id, ai_builds = await _seed_human_lane_game(
        session_factory, human_seat_ids=human_seats
    )
    await _persist_human_seat_rows(
        session_factory,
        game_id=game_id,
        human_seat_ids=human_seats,
        takeover_seat_id=takeover_seat,
        ai_builds=ai_builds,
    )

    clock = _FakeClock()
    mux = _mixed_adapter(human_seats, script, clock)
    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_id,
        agent_builds=ai_builds,
        league_id=league_id,
    )
    outcome = await run_game(
        GameConfig(game_id="G-INV", game_seed=_GAME_SEED, timeout_s=1.0),
        mux,
        ranked=False,  # human lane is ALWAYS casual.
        persistence=persistence,
    )
    assert outcome.final_state.terminal_result == "TOWN"

    return _PlayedGame(
        game_id=game_id,
        league_id=league_id,
        ai_builds=ai_builds,
        human_seats=human_seats,
        takeover_seat=takeover_seat,
    )


# --------------------------------------------------------------------------- #
# SEGREGATION gate
# --------------------------------------------------------------------------- #


async def test_segregation_human_game_writes_zero_scientific_rating_rows(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A completed human-lane game writes ZERO rows to ANY rating table."""
    counts = await _count_rating_rows(session_factory)
    assert counts == (0, 0, 0, 0)


# --------------------------------------------------------------------------- #
# ANONYMITY gate: every pre-reveal read surface is identity-blind
# --------------------------------------------------------------------------- #


async def _raw_events(
    session_factory: async_sessionmaker[AsyncSession],
    game_id: uuid.UUID,
) -> list[dict[str, object]]:
    from padrino.db.models import GameEvent

    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(GameEvent)
                    .where(GameEvent.game_id == game_id)
                    .order_by(GameEvent.sequence)
                )
            ).scalars()
        )
    return [
        {
            "sequence": e.sequence,
            "event_type": e.event_type,
            "phase": e.phase,
            "visibility": e.visibility,
            "actor_player_id": e.actor_player_id,
            "payload": dict(e.payload) if e.payload else {},
            "prev_event_hash": e.prev_event_hash,
            "event_hash": e.event_hash,
        }
        for e in rows
    ]


async def test_anonymity_public_event_v1_projection_is_identity_blind(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every public_event_v1 frame of the human game leaks zero forbidden keys."""
    raw = await _raw_events(session_factory, played_human_game.game_id)
    frames = to_public_events_v1(raw)
    assert frames  # the game produced public frames
    for frame in frames:
        assert_anonymous_safe(frame)
        assert "terminal_result" not in frame


async def test_anonymity_spectator_projection_is_identity_blind(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The spectator projection of the human game leaks zero forbidden keys."""
    raw = await _raw_events(session_factory, played_human_game.game_id)
    for frame in project_events_for_spectator(raw):
        assert_anonymous_safe(frame)


async def test_anonymity_live_tail_stream_is_identity_blind(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every live-tail SSE frame of the human game leaks zero forbidden keys."""
    cfg = LiveTailConfig(poll_ms=1, heartbeat_ms=1_000_000, idle_timeout_ms=5_000)
    frame_count = 0
    async for block in stream_live_tail(session_factory, played_human_game.game_id, config=cfg):
        for line in block.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[len("data:") :].strip())
                assert_anonymous_safe(payload)
                frame_count += 1
    assert frame_count  # the live tail actually emitted frames to audit


async def test_anonymity_per_seat_observation_guard_has_no_identity_markers(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No public frame carries a human-vs-AI / model identity marker."""
    raw = await _raw_events(session_factory, played_human_game.game_id)
    for frame in to_public_events_v1(raw):
        assert_no_identity_markers(frame)


async def test_anonymity_export_bundle_is_payload_safe(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The export bundle of the completed human game carries no identity leak.

    The export bundle IS the scientific terminal archive: it legitimately
    carries per-seat ``role`` / ``faction`` (that is the whole point of the
    record), so the broad pre-reveal ``assert_anonymous_safe`` does not apply
    here. The export-path invariant is narrower: no model / provider /
    agent-build identifier (``assert_bundle_payload_safe``) and no human-vs-AI /
    model-identity marker (``assert_no_identity_markers``) ever reaches an event
    payload, and the seat-info shape carries NO human-identity column at all.
    """
    async with session_factory() as session:
        bundle = await export_game(session, played_human_game.game_id)
    # assert_bundle_payload_safe raises on any forbidden key; calling it twice
    # (export already calls it internally) documents the invariant explicitly.
    assert_bundle_payload_safe(bundle.events)
    for event in bundle.events:
        assert_no_identity_markers(event.payload)
    # The bundle's per-seat info exposes role/faction (scientific record) but
    # carries ZERO human-identity columns (seat_kind, occupant_*, takeover_*).
    for seat_info in bundle.game_seats:
        fields = set(seat_info.model_dump())
        assert not (HUMAN_IDENTITY_KEYS & fields)


async def test_anonymity_seat_and_game_row_projections_drop_identity_columns(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The column-level guard drops every identity column from real seat/game rows."""
    async with session_factory() as session:
        game = await session.get(Game, played_human_game.game_id)
        assert game is not None
        seats = list(
            (
                await session.execute(
                    select(GameSeat).where(GameSeat.game_id == played_human_game.game_id)
                )
            ).scalars()
        )
    game_row = {c.name: getattr(game, c.name) for c in Game.__table__.columns}
    projected_game = project_game_row(game_row)
    assert "identity_mode" not in projected_game
    assert_anonymous_safe(projected_game)

    for seat in seats:
        seat_row = {c.name: getattr(seat, c.name) for c in GameSeat.__table__.columns}
        # The raw seat row DOES carry identity columns...
        assert HUMAN_IDENTITY_KEYS & set(seat_row)
        projected = project_seat_row(seat_row)
        # ...but the projection drops every one of them.
        assert not (HUMAN_IDENTITY_KEYS & set(projected))
        assert not (IDENTITY_MARKER_KEYS & set(projected))
        assert_anonymous_safe(projected)


# --------------------------------------------------------------------------- #
# REVEAL gate: pre-terminal hides truth; terminal (RECENT) discloses it
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    get_settings.cache_clear()


async def _spectator_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[AsyncClient, str]:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        from padrino.db.repositories import api_keys as api_keys_repo

        await api_keys_repo.create(
            session, raw_key=raw, scopes=[SCOPE_SPECTATOR], label="invariant-viewer"
        )
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
    return client, raw


async def test_reveal_is_hidden_pre_terminal_and_discloses_truth_when_terminal(
    played_human_game: _PlayedGame,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The endgame reveal is the ONLY surface that discloses identity, and only
    once the game is terminal (RECENT). While LIVE it 404s; once RECENT it
    reveals which seats were human, roles, models, and takeover provenance."""
    client, raw = await _spectator_client(session_factory)
    headers = {"Authorization": f"Bearer {raw}"}
    try:
        # Pre-terminal (the game is still LIVE): reveal must 404.
        async with session_factory() as session, session.begin():
            game = await session.get(Game, played_human_game.game_id)
            assert game is not None
            game.broadcast_state = BroadcastState.LIVE.value
        r = await client.get(f"/public/games/{played_human_game.game_id}/reveal", headers=headers)
        assert r.status_code == 404

        # Flip to RECENT (broadcast complete) and the reveal opens.
        async with session_factory() as session, session.begin():
            game = await session.get(Game, played_human_game.game_id)
            assert game is not None
            game.broadcast_state = BroadcastState.RECENT.value

        r = await client.get(f"/public/games/{played_human_game.game_id}/reveal", headers=headers)
        assert r.status_code == 200
        body = r.json()
        seats = {s["public_player_id"]: s for s in body["seats"]}

        # Human-held seats reveal as human (decision 11: reveal always discloses).
        for sid in played_human_game.human_seats:
            assert seats[sid]["is_human"] is True
            assert seats[sid]["takeover_provenance"] == "HUMAN"
            assert seats[sid]["model"] is None

        # The taken-over seat reveals HUMAN_THEN_AI provenance + the finishing AI.
        taken = seats[played_human_game.takeover_seat]
        assert taken["is_human"] is False
        assert taken["takeover_provenance"] == "HUMAN_THEN_AI"

        # At least one pure-AI seat reveals its exact model.
        ai_seats = [
            s
            for sid, s in seats.items()
            if sid not in played_human_game.human_seats and sid != played_human_game.takeover_seat
        ]
        assert ai_seats
        assert any(s["model"] is not None for s in ai_seats)
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# Re-run the US-124 / US-125 scaffolds inside this aggregate gate, so a single
# `pytest tests/test_invariants_human_mp.py` enforces both invariants directly.
# --------------------------------------------------------------------------- #


async def test_assert_anonymous_safe_catches_every_human_identity_marker() -> None:
    for key in sorted(HUMAN_IDENTITY_KEYS):
        with pytest.raises(AnonymityViolation):
            assert_anonymous_safe({"seats": [{key: True}]})


async def test_assert_no_identity_markers_catches_model_and_human_markers() -> None:
    for key in sorted(IDENTITY_MARKER_KEYS):
        with pytest.raises(AnonymityViolation):
            assert_no_identity_markers({"meta": {key: "x"}})
