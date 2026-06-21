"""End-to-end human-game smoke test (Wave 9, US-157).

The single default-suite guard that exercises the ENTIRE human spine through the
REAL HTTP channels, so a cross-boundary contract break can never pass a green
suite (the Wave-7 lesson). One test walks:

    create lobby -> guest join -> consent -> ready -> lock -> launch (curated
    auto-fill materializes a real human-lane Game + GameSeat rows) -> the human
    worker lane drives the game while a scripted human client POSTs the host's
    structured actions (votes + night actions) and one moderated public chat
    message over the authenticated channels -> the game reaches a terminal
    result -> the broadcast completes -> the endgame reveal exposes models +
    human seats -> a spot-the-AI guess scores.

The human seat is NOT shortcut: its turn is resolved by polling the buffered
POST action channel (US-134) exactly like production. A scripted human client
coroutine POSTs the host's action for the observed phase through the real
``/human/games/{id}/actions`` endpoint; the runner's :class:`HumanAdapter` then
pulls that buffered submission back from the DB. AI seats are driven by a
deterministic mock script. Everything runs under an injected fake clock with
zero/short delays, so the whole spine is deterministic and never touches a real
LLM or the wall clock for game timing.

Two invariants are asserted across the whole spine (the Wave-9 non-negotiables):

* ANONYMITY held on every live frame (no forbidden human-vs-AI / model keys),
  and the reveal is the ONLY surface that opens identity (a LIVE game 404s).
* ZERO rows were written to the scientific ``ratings`` / ``rating_events``
  tables (and zero to the dormant human-rating siblings, casual v1).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_SPECTATOR, RateLimiter, generate_raw_key
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.core.agents.contract import AgentResponse
from padrino.core.engine.actions import Action
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Seat
from padrino.core.enums import ActionType, Faction, Role, SeatKind
from padrino.core.human_chat import human_chat_content_ref
from padrino.core.observation_privacy import assert_anonymous_safe
from padrino.core.observations import Observation, Ruleset
from padrino.db.models import (
    Game,
    GameEvent,
    GameSeat,
    HumanActionSubmission,
    HumanChatMessage,
    HumanRating,
    HumanRatingEvent,
    Rating,
    RatingEvent,
)
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.llm.human_adapter import HumanAdapter, PullAction
from padrino.llm.mock import DeterministicMockAdapter
from padrino.llm.multiplex import SeatMultiplexAdapter
from padrino.public.broadcast_index import BroadcastState
from padrino.public.live_tail import LiveTailConfig, stream_live_tail
from padrino.runner.game_runner import GameConfig, GamePersistence, drive_game_loop
from padrino.runner.human_chat_release import release_held_chat_for_phase
from padrino.runner.human_lane import _run_human_tick_responses
from padrino.runner.human_tick import HumanTickConfig
from tests.conftest import make_town_win_script

_RULESET = "mini7_v1"
_PLAYER_COUNT = 7
_AI_SEAT_COUNT = _PLAYER_COUNT - 1
# The host occupies lobby seat 0 -> public_player_id P01 (assign_roles maps
# seat_index i -> P0{i+1}). The host is the human player in this game.
_HOST_SEAT = "P01"


# --------------------------------------------------------------------------- #
# Deterministic clock (advances only when the injected sleep is awaited)
# --------------------------------------------------------------------------- #


class _FakeClock:
    def __init__(self) -> None:
        self._now = 0.0

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self._now += seconds


class _ScriptedSeatAdapter:
    """An LLM adapter returning one AI seat's slice of a phase-keyed script."""

    def __init__(self, seat_id: str, script: Mapping[tuple[str, str], AgentResponse]) -> None:
        self._inner = DeterministicMockAdapter(
            {key: resp for key, resp in script.items() if key[1] == seat_id}
        )

    async def complete(self, observation: Observation) -> AdapterResult:
        return await self._inner.complete(observation)


# --------------------------------------------------------------------------- #
# HTTP helpers (guest auth, consent, lobby spine)
# --------------------------------------------------------------------------- #


def _guest_token(resp_headers: object) -> str:
    jar = SimpleCookie()
    for raw in resp_headers.get_list("set-cookie"):  # type: ignore[attr-defined]
        if raw.startswith(f"{HUMAN_SESSION_COOKIE}="):
            jar.load(raw)
    return jar[HUMAN_SESSION_COOKIE].value


async def _new_guest(client: AsyncClient) -> str:
    resp = await client.post("/human/guest")
    assert resp.status_code == 201, resp.text
    return _guest_token(resp.headers)


async def _consent(client: AsyncClient, token: str) -> None:
    resp = await client.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: token})
    assert resp.status_code == 201, resp.text


async def _seed_curated_pool(session: AsyncSession, *, count: int) -> None:
    """Seed ``count`` active curated builds targeting mini7_v1 (the auto-fill pool)."""
    from padrino.db.models import AgentBuild, ModelConfig, ModelProvider, PromptVersion

    provider = ModelProvider(name="cerebras", base_url=None, auth_secret_ref="CEREBRAS_API_KEY")
    session.add(provider)
    await session.flush()
    mc = ModelConfig(
        provider_id=provider.id,
        model_name="zai-glm-4.7",
        model_version=None,
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=4096,
        supports_structured_outputs=True,
    )
    session.add(mc)
    pv = PromptVersion(
        ruleset_id=_RULESET,
        version="v1",
        system_prompt="play",
        developer_prompt="json",
        response_schema={"type": "object"},
        prompt_hash="hash-us157",
    )
    session.add(pv)
    await session.flush()
    for i in range(count):
        session.add(
            AgentBuild(
                display_name=f"cerebras/glm-4.7@v1-{i}",
                model_config_id=mc.id,
                prompt_version_id=pv.id,
                adapter_version="2026.05",
                inference_params={"temperature": 0.7},
                active=True,
            )
        )
    await session.commit()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


@pytest.fixture(autouse=True)
def _pristine_settings() -> Iterator[None]:
    """Use pristine default settings so lobby admission caps are deterministic.

    Other suites pin tiny caps / breaker thresholds via env + ``get_settings``
    cache flips; clearing the cache before and after this test keeps the
    cumulative-cost admission gate from inheriting a leaked low cap that would
    deny lobby creation only when this test runs after them in the full suite.
    """
    from padrino.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


# --------------------------------------------------------------------------- #
# Game-driving glue: build the script + the DB-backed human pull_action
# --------------------------------------------------------------------------- #


async def _seat_rows(
    session_factory: async_sessionmaker[AsyncSession], game_id: uuid.UUID
) -> list[GameSeat]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(GameSeat)
                    .where(GameSeat.game_id == game_id)
                    .order_by(GameSeat.seat_index)
                )
            ).scalars()
        )


def _town_win_script_from_seats(seats: list[GameSeat]) -> dict[tuple[str, str], AgentResponse]:
    """Build a TOWN-win script from the actually-assigned seat roles.

    The lobby (and hence game) seed is random, so roles are only known after the
    launch handoff materializes the seat rows. The script makes whichever seats
    hold each role follow the canonical town-win plan; the host seat follows its
    own assigned role's scripted actions just like an AI would.
    """
    mafia = [s.public_player_id for s in seats if s.faction == Faction.MAFIA.value]
    town = [s.public_player_id for s in seats if s.faction == Faction.TOWN.value]
    doctor = next(s.public_player_id for s in seats if s.role == Role.DOCTOR.value)
    detective = next(s.public_player_id for s in seats if s.role == Role.DETECTIVE.value)
    return make_town_win_script(
        mafia_ids=mafia, town_ids=town, doctor_id=doctor, detective_id=detective
    )


def _db_backed_human_pull(
    client: AsyncClient,
    token: str,
    game_id: uuid.UUID,
    script: Mapping[tuple[str, str], AgentResponse],
    *,
    chat_sent: list[bool],
) -> PullAction:
    """Return a ``pull_action`` that drives the host seat over the REAL POST channels.

    For the observed phase it POSTs the host's scripted action through
    ``/human/games/{id}/actions`` (the authenticated US-134 channel), and on the
    FIRST discussion phase the host is alive for, it also POSTs exactly ONE
    moderated public chat message through ``/human/games/{id}/chat`` (US-135).
    The buffered action is then returned to the runner's :class:`HumanAdapter`,
    exactly mirroring how the production human-aware tick resolves a human seat
    from buffered input. ``chat_sent`` is a single-element mutable flag so the
    one-message guard is independent of how many discussion phases the
    (random-seed) game runs — a strictly-once post regardless of game length.
    """

    async def pull(observation: Observation) -> Action | None:
        phase = observation.phase
        scripted = script.get((phase, _HOST_SEAT))
        if scripted is None or scripted.action is None:
            return None

        # Moderated public chat: exactly ONE human-authored message, sent the
        # first time the host is asked to act in a discussion phase.
        if "_DISCUSSION_" in phase and not chat_sent[0]:
            chat_sent[0] = True
            chat = await client.post(
                f"/human/games/{game_id}/chat",
                json={
                    "channel": "PUBLIC",
                    "text": "I think we should vote carefully today.",
                    "idempotency_key": "chat-once",
                },
                cookies={HUMAN_SESSION_COOKIE: token},
            )
            assert chat.status_code == 200, chat.text
            assert chat.json()["status"] == "HELD"

        action = scripted.action
        body: dict[str, object] = {"type": action.type.value}
        if action.target is not None:
            body["target"] = action.target
        resp = await client.post(
            f"/human/games/{game_id}/actions",
            json={"action": body, "idempotency_key": f"act-{phase}"},
            cookies={HUMAN_SESSION_COOKIE: token},
        )
        # A discussion phase has no votable/structured action for the seat; the
        # POST channel rejects it (409) and the human simply talks, not acts.
        if resp.status_code != 200:
            return None
        return action

    return pull


def _mixed_adapter(
    seats: list[GameSeat],
    script: Mapping[tuple[str, str], AgentResponse],
    human_pull: PullAction,
    clock: _FakeClock,
) -> SeatMultiplexAdapter:
    adapters: dict[str, LlmAdapter] = {}
    for seat in seats:
        sid = seat.public_player_id
        if sid == _HOST_SEAT:
            adapters[sid] = HumanAdapter(
                pull_action=human_pull,
                deadline_seconds=30.0,
                poll_interval_seconds=0.5,
                clock=clock.now,
                sleep=clock.sleep,
            )
        else:
            adapters[sid] = _ScriptedSeatAdapter(sid, script)
    return SeatMultiplexAdapter(adapters)


# --------------------------------------------------------------------------- #
# The end-to-end smoke test
# --------------------------------------------------------------------------- #


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


async def _assert_live_frames_anonymous(
    session_factory: async_sessionmaker[AsyncSession], game_id: uuid.UUID
) -> None:
    """Every live-tail SSE frame of the human game leaks zero forbidden keys."""
    cfg = LiveTailConfig(poll_ms=1, heartbeat_ms=1_000_000, idle_timeout_ms=5_000)
    frame_count = 0
    async for block in stream_live_tail(session_factory, game_id, config=cfg):
        for line in block.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[len("data:") :].strip())
                assert_anonymous_safe(payload)
                frame_count += 1
    assert frame_count  # the live tail actually emitted frames to audit


@pytest.mark.asyncio
async def test_human_game_full_spine_smoke(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # ---- Curated pool so launch auto-fill can fill the 6 AI seats. ----------
    async with session_factory() as session:
        await _seed_curated_pool(session, count=_AI_SEAT_COUNT)

    # ---- create lobby (host) ------------------------------------------------
    host = await _new_guest(client)
    await _consent(client, host)
    created = await client.post(
        "/lobbies",
        json={"ruleset_id": _RULESET},
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert created.status_code == 201, created.text
    lobby = created.json()
    lobby_id = uuid.UUID(lobby["id"])
    invite_token = lobby["invite_token"]
    # Composition is disclosed counts-only, never a per-seat human/AI map.
    assert "seats" not in lobby
    assert lobby["composition"]["total"] == _PLAYER_COUNT

    # ---- guest join (a friend joins the private lobby) ----------------------
    guest = await _new_guest(client)
    await _consent(client, guest)
    joined = await client.post(
        f"/lobbies/join/{invite_token}",
        cookies={HUMAN_SESSION_COOKIE: guest},
    )
    assert joined.status_code == 200, joined.text
    roster = await client.get(f"/lobbies/{lobby_id}/roster", cookies={HUMAN_SESSION_COOKIE: host})
    assert roster.status_code == 200
    # The roster is identity-blind: members carry no seat_kind / principal / map.
    for member in roster.json()["members"]:
        assert set(member) == {"member_id", "is_host", "ready", "present"}
    assert roster.json()["member_count"] == 2

    # ---- ready -> lock -> launch (curated auto-fill + handoff) --------------
    ready = await client.post(
        f"/lobbies/{lobby_id}/ready",
        json={"ready": True},
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert ready.status_code == 200, ready.text
    locked = await client.post(f"/lobbies/{lobby_id}/lock", cookies={HUMAN_SESSION_COOKIE: host})
    assert locked.status_code == 200, locked.text
    launched = await client.post(
        f"/lobbies/{lobby_id}/launch", cookies={HUMAN_SESSION_COOKIE: host}
    )
    assert launched.status_code == 200, launched.text
    assert launched.json()["created"] is True
    game_id = uuid.UUID(launched.json()["game_id"])

    # The materialized game is gauntlet-less (benchmark scheduler never claims it)
    # and carries exactly one HUMAN seat (the host) + six curated AI seats.
    seats = await _seat_rows(session_factory, game_id)
    assert len(seats) == _PLAYER_COUNT
    human_seats = [s for s in seats if s.seat_kind == SeatKind.HUMAN.value]
    assert len(human_seats) == 1
    assert human_seats[0].public_player_id == _HOST_SEAT
    assert human_seats[0].occupant_principal_id is not None

    # ---- play a full game through the human lane ----------------------------
    # Mark the game LIVE so the live-tail / reveal gating is exercised against
    # the real columns (the broadcast lifecycle is separate from the engine).
    async with session_factory() as session, session.begin():
        game = await session.get(Game, game_id)
        assert game is not None
        game.broadcast_state = BroadcastState.LIVE.value
        game.is_broadcastable = True

    script = _town_win_script_from_seats(seats)
    clock = _FakeClock()
    human_pull = _db_backed_human_pull(client, host, game_id, script, chat_sent=[False])
    mux = _mixed_adapter(seats, script, human_pull, clock)

    async with session_factory() as session, session.begin():
        game = await session.get(Game, game_id)
        assert game is not None
        game_seed = game.game_seed

    persistence = GamePersistence(
        session_factory=session_factory,
        game_id=game_id,
        agent_builds={},  # human-lane: empty -> rating write path fails closed
        league_id=None,
    )
    release_base = datetime(2026, 6, 20, tzinfo=UTC)

    async def release_chat(phase: str, settled_at: float, release_log: EventLog) -> None:
        async with session_factory() as session, session.begin():
            await release_held_chat_for_phase(
                session,
                game_id=game_id,
                phase=phase,
                released_at=release_base + timedelta(seconds=settled_at),
                event_log=release_log,
            )

    async def tick_runner(
        state: GameState,
        event_log: EventLog,
        eligible_seats: Sequence[Seat],
        tick_adapter: LlmAdapter,
        ruleset: Ruleset,
        ranked: bool,
        timeout_s: float,
    ) -> dict[str, AgentResponse]:
        return await _run_human_tick_responses(
            state,
            event_log,
            eligible_seats,
            tick_adapter,
            ruleset,
            ranked,
            timeout_s,
            config=HumanTickConfig(phase_deadline_seconds=1.0, release_delay_seconds=0.0),
            clock=clock.now,
            sleep=clock.sleep,
            release_chat=release_chat,
        )

    outcome = await drive_game_loop(
        GameConfig(game_id=str(game_id), game_seed=game_seed, ruleset_id=_RULESET, timeout_s=1.0),
        mux,
        ranked=False,
        persistence=persistence,
        tick_runner=tick_runner,
    )

    # ---- terminal -----------------------------------------------------------
    assert outcome.final_state.terminal_result == "TOWN"
    async with session_factory() as session:
        game = await session.get(Game, game_id)
        assert game is not None
        assert game.status == "COMPLETED"
        assert isinstance(game.terminal_result, dict)
        assert game.terminal_result["winner"] == "TOWN"

    # The human's moderated chat message was held, released, and routed to the
    # out-of-band sidecar (US-123) - never inline in the hash-chained log.
    async with session_factory() as session:
        sidecar = (
            (
                await session.execute(
                    select(HumanChatMessage).where(HumanChatMessage.game_id == game_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(sidecar) == 1
    assert sidecar[0].public_player_id == _HOST_SEAT
    assert sidecar[0].raw_text == "I think we should vote carefully today."
    async with session_factory() as session:
        chained = (
            (
                await session.execute(
                    select(GameEvent)
                    .where(GameEvent.game_id == game_id)
                    .where(GameEvent.sequence == sidecar[0].sequence)
                )
            )
            .scalars()
            .one()
        )
    assert chained.event_type == "PublicMessageSubmitted"
    assert chained.actor_player_id == _HOST_SEAT
    assert chained.payload == {
        "text": "",
        "round_index": 1,
        "content_ref": human_chat_content_ref("I think we should vote carefully today."),
    }
    assert "I think we should vote carefully today." not in str(chained.payload)

    # The host's structured actions genuinely flowed through the authenticated
    # POST action channel (US-134) - the human seat was driven, not shortcut.
    async with session_factory() as session:
        host_actions = (
            (
                await session.execute(
                    select(HumanActionSubmission).where(
                        HumanActionSubmission.game_id == game_id,
                        HumanActionSubmission.public_player_id == _HOST_SEAT,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert host_actions  # at least one vote / night action was buffered via POST
    assert all(a.action_type in {t.value for t in ActionType} for a in host_actions)

    # ---- anonymity held on every live frame --------------------------------
    await _assert_live_frames_anonymous(session_factory, game_id)

    # ---- SEGREGATION: zero scientific rating rows --------------------------
    assert await _count_rating_rows(session_factory) == (0, 0, 0, 0)

    # ---- reveal is hidden while LIVE, opens once the broadcast completes -----
    raw_key = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(
            session, raw_key=raw_key, scopes=[SCOPE_SPECTATOR], label="e2e-viewer"
        )
    auth = {"Authorization": f"Bearer {raw_key}"}

    pre = await client.get(f"/public/games/{game_id}/reveal", headers=auth)
    assert pre.status_code == 404  # still LIVE -> the truth is not disclosed early

    async with session_factory() as session, session.begin():
        game = await session.get(Game, game_id)
        assert game is not None
        game.broadcast_state = BroadcastState.RECENT.value

    revealed = await client.get(f"/public/games/{game_id}/reveal", headers=auth)
    assert revealed.status_code == 200, revealed.text
    body = revealed.json()
    assert body["winner"] == "TOWN"
    reveal_seats = {s["public_player_id"]: s for s in body["seats"]}

    # The host's HUMAN seat reveals as human (no model).
    assert reveal_seats[_HOST_SEAT]["is_human"] is True
    assert reveal_seats[_HOST_SEAT]["takeover_provenance"] == "HUMAN"
    assert reveal_seats[_HOST_SEAT]["model"] is None
    # Every AI seat reveals its exact model identity.
    ai_reveals = [s for sid, s in reveal_seats.items() if sid != _HOST_SEAT]
    assert len(ai_reveals) == _AI_SEAT_COUNT
    assert all(s["is_human"] is False for s in ai_reveals)
    assert all(s["model"] is not None for s in ai_reveals)
    assert all(s["model"]["model_name"] == "zai-glm-4.7" for s in ai_reveals)

    # ---- spot-the-AI guess scores ------------------------------------------
    # The host guesses every OTHER seat is AI (the truth, since the host is the
    # only human), so the guess scores a perfect detection accuracy.
    guess = {sid: "AI" for sid in reveal_seats if sid != _HOST_SEAT}
    guess_resp = await client.post(
        f"/human/games/{game_id}/turing-guess",
        json={"guess": guess},
        cookies={HUMAN_SESSION_COOKIE: host},
    )
    assert guess_resp.status_code == 200, guess_resp.text
    score = guess_resp.json()
    assert score["guesser_public_id"] == _HOST_SEAT
    assert score["total"] == _AI_SEAT_COUNT
    assert score["correct"] == _AI_SEAT_COUNT
    assert score["idempotent_replay"] is False

    # The personal accuracy is disclosed only after the guess was submitted.
    own = await client.get(
        f"/human/games/{game_id}/turing-guess", cookies={HUMAN_SESSION_COOKIE: host}
    )
    assert own.status_code == 200
    assert own.json()["correct"] == _AI_SEAT_COUNT
