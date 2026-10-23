"""US-409: local TTS endpoint for public frames. No real TTS or network calls."""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import RateLimiter
from padrino.api.routes import speech
from padrino.db.models import Game, GameEvent
from padrino.public.broadcast_index import BroadcastState
from padrino.settings import Settings, get_settings

SECRET = "sk-super-secret-key-123"
WAV = b"RIFFfakewav"


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    get_settings.cache_clear()


class Calls:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail = False


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    c = Calls()

    async def fake_say(voice: str, text: str) -> bytes:
        c.calls.append(("say", voice, text))
        await asyncio.sleep(0.01)
        if c.fail:
            raise speech.SpeechError("tts_say_failed")
        return WAV

    monkeypatch.setattr(speech, "_run_say", fake_say)
    return c


def _build(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path, **kw: Any
) -> tuple[Any, AsyncClient]:
    app = create_app(
        session_factory=session_factory, auth_required=False, rate_limiter=RateLimiter()
    )
    base = {"padrino_tts_provider": "say", "padrino_tts_cache_dir": str(tmp_path / "cache")}
    app.state.auth_settings = Settings(**{**base, **kw})
    client = AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://t"
    )
    return app, client


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> AsyncIterator[AsyncClient]:
    _, c = _build(session_factory, tmp_path)
    async with c:
        yield c


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    events: list[tuple[str, str, str, str | None, dict[str, Any]]],
    *,
    broadcastable: bool = True,
    state: str = BroadcastState.LIVE.value,
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        g = Game(
            ruleset_id="mini7_v1",
            game_seed="seed-409",
            status="RUNNING",
            broadcast_state=state,
            is_broadcastable=broadcastable,
        )
        session.add(g)
        await session.flush()
        for i, (etype, phase, vis, actor, payload) in enumerate(events, start=1):
            session.add(
                GameEvent(
                    game_id=g.id,
                    sequence=i,
                    event_type=etype,
                    phase=phase,
                    visibility=vis,
                    actor_player_id=actor,
                    payload=payload,
                    prev_event_hash="0" * 64,
                    event_hash=f"{i:064x}",
                )
            )
        return g.id


def _chat(actor: str, text: str, **extra: Any) -> tuple[str, str, str, str | None, dict[str, Any]]:
    return (
        "PublicMessageSubmitted",
        "DAY_1_DISCUSSION_ROUND_1",
        "PUBLIC",
        actor,
        {"text": text, **extra},
    )


STD = [
    _chat("P03", "I suspect P05."),  # 1
    (
        "PlayerEliminated",
        "DAY_1_VOTE",
        "PUBLIC",
        None,
        {"public_player_id": "P05", "cause": "VOTE", "role": "MAFIA"},
    ),  # 2
    ("GameTerminated", "TERMINAL", "PUBLIC", None, {"winner": "TOWN", "reason": "x"}),  # 3
    ("PhaseStarted", "NIGHT_2_ACTIONS", "SYSTEM", None, {}),  # 4
    (
        "PrivateMessageSubmitted",
        "NIGHT_1_MAFIA_DISCUSSION",
        "PRIVATE",
        "P01",
        {"text": "secret"},
    ),  # 5
]


async def test_off_by_default(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path, calls: Calls
) -> None:
    gid = await _seed(session_factory, STD)
    _, c = _build(session_factory, tmp_path, padrino_tts_provider=None)
    async with c:
        r = await c.get(f"/public/games/{gid}/speech/1")
    assert r.status_code == 404
    assert calls.calls == []


async def test_non_loopback_403(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path, calls: Calls
) -> None:
    gid = await _seed(session_factory, STD)
    app, _ = _build(session_factory, tmp_path)
    transport = ASGITransport(app=app, client=("203.0.113.9", 1))
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(f"/public/games/{gid}/speech/1")
    assert r.status_code == 403
    assert calls.calls == []


@pytest.mark.parametrize(
    ("broadcastable", "state"),
    [(False, "LIVE"), (True, "NONE"), (True, "ARCHIVED")],
)
async def test_gate_404(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncClient,
    calls: Calls,
    broadcastable: bool,
    state: str,
) -> None:
    gid = await _seed(session_factory, STD, broadcastable=broadcastable, state=state)
    assert (await client.get(f"/public/games/{gid}/speech/1")).status_code == 404
    assert (await client.get(f"/public/games/{uuid.uuid4()}/speech/1")).status_code == 404
    assert calls.calls == []


@pytest.mark.parametrize("seq", [4, 5, 99])
async def test_private_system_missing_404(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls, seq: int
) -> None:
    gid = await _seed(session_factory, STD)
    for cue in ("chat", "phase"):
        r = await client.get(f"/public/games/{gid}/speech/{seq}?cue={cue}")
        assert r.status_code == 404
    assert calls.calls == []


async def test_chat_uses_seat_voice_and_text(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls
) -> None:
    gid = await _seed(session_factory, STD)
    r = await client.get(f"/public/games/{gid}/speech/1")
    assert r.status_code == 200
    assert r.content == WAV
    assert r.headers["content-type"] == "audio/wav"
    assert r.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert calls.calls == [("say", speech.SAY_SEAT_VOICES[2], "I suspect P05.")]


async def test_narrator_templates(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls
) -> None:
    gid = await _seed(session_factory, STD)
    # cue=phase on an elimination frame announces the phase, not the elimination
    for seq, cue in ((2, "chat"), (3, "chat"), (1, "phase"), (2, "phase")):
        assert (await client.get(f"/public/games/{gid}/speech/{seq}?cue={cue}")).status_code == 200
    narr = speech.SAY_NARRATOR_VOICE
    assert calls.calls == [
        ("say", narr, "P05 has been eliminated."),
        ("say", narr, "The game is over. TOWN wins."),
        ("say", narr, "Day 1 begins."),
        ("say", narr, "Day 1, voting begins."),
    ]
    assert speech.phase_text("DAY_1_DISCUSSION_ROUND_1") == "Day 1 begins."
    assert speech.phase_text("NIGHT_2_ACTIONS") == "Night 2 falls."
    assert speech.phase_text("WEIRD") == "A new phase begins."


async def test_bad_cue_422(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls
) -> None:
    gid = await _seed(session_factory, STD)
    assert (await client.get(f"/public/games/{gid}/speech/1?cue=evil")).status_code == 422


async def test_voice_independent_of_model(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls
) -> None:
    a = await _seed(session_factory, [_chat("P04", "hello", model="gpt-x", provider="openai")])
    b = await _seed(
        session_factory, [_chat("P04", "hello", model="claude-y", provider="anthropic")]
    )
    await client.get(f"/public/games/{a}/speech/1")
    await client.get(f"/public/games/{b}/speech/1")
    assert {c[1] for c in calls.calls} <= {speech.SAY_SEAT_VOICES[3]}
    for p in ("say", "gcp", "elevenlabs"):
        assert len(set(speech._VOICES[p][0])) == 7
        assert speech._VOICES[p][1] not in speech._VOICES[p][0]


async def test_cache_hit_skips_synthesis(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls
) -> None:
    gid = await _seed(session_factory, STD)
    r1 = await client.get(f"/public/games/{gid}/speech/1")
    r2 = await client.get(f"/public/games/{gid}/speech/1")
    assert r1.content == r2.content == WAV
    assert len(calls.calls) == 1


async def test_cache_hit_still_gated(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls
) -> None:
    gid = await _seed(session_factory, STD)
    assert (await client.get(f"/public/games/{gid}/speech/1")).status_code == 200
    async with session_factory() as session, session.begin():
        g = await session.get(Game, gid)
        assert g is not None
        g.is_broadcastable = False
    assert (await client.get(f"/public/games/{gid}/speech/1")).status_code == 404


async def test_concurrent_identical_requests_synthesize_once(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls
) -> None:
    gid = await _seed(session_factory, STD)
    rs = await asyncio.gather(*(client.get(f"/public/games/{gid}/speech/1") for _ in range(5)))
    assert all(r.status_code == 200 for r in rs)
    assert len(calls.calls) == 1


async def test_text_capped(
    session_factory: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls
) -> None:
    gid = await _seed(session_factory, [_chat("P01", "x" * 5000)])
    await client.get(f"/public/games/{gid}/speech/1")
    assert len(calls.calls[0][2]) == speech.MAX_TEXT_CHARS


async def test_provider_failure_502_sanitized(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncClient,
    calls: Calls,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls.fail = True
    gid = await _seed(session_factory, STD)
    with caplog.at_level(logging.DEBUG):
        r = await client.get(f"/public/games/{gid}/speech/1")
    assert r.status_code == 502
    assert not list((tmp_path / "cache").glob("*")) if (tmp_path / "cache").exists() else True


def _mock(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_elevenlabs_success_and_failure_no_secret_leak(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", SECRET)
    seen: list[httpx.Request] = []
    mode = {"fail": False}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if mode["fail"]:
            return httpx.Response(401, text=f"bad key {SECRET}")
        return httpx.Response(200, content=b"ID3mp3")

    monkeypatch.setattr(speech, "_http_transport", _mock(handler))
    gid = await _seed(session_factory, STD)
    _, c = _build(session_factory, tmp_path, padrino_tts_provider="elevenlabs")
    async with c:
        r = await c.get(f"/public/games/{gid}/speech/1")
        assert r.status_code == 200
        assert r.content == b"ID3mp3"
        assert r.headers["content-type"] == "audio/mpeg"
        assert seen[0].headers["xi-api-key"] == SECRET
        assert str(seen[0].url).endswith(f"/v1/text-to-speech/{speech.ELEVENLABS_SEAT_VOICES[2]}")
        mode["fail"] = True
        with caplog.at_level(logging.DEBUG):
            r = await c.get(f"/public/games/{gid}/speech/2")
    assert r.status_code == 502
    assert SECRET not in r.text
    assert SECRET not in caplog.text


async def test_elevenlabs_missing_key_502(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    gid = await _seed(session_factory, STD)
    _, c = _build(session_factory, tmp_path, padrino_tts_provider="elevenlabs")
    async with c:
        r = await c.get(f"/public/games/{gid}/speech/1")
    assert r.status_code == 502


async def test_gcp_success_and_api_disabled(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "ya29.tok-secret"

    async def fake_token() -> str:
        return token

    monkeypatch.setattr(speech, "_gcloud_token", fake_token)
    seen: list[httpx.Request] = []
    mode = {"disabled": False}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if mode["disabled"]:
            return httpx.Response(403, text=f"API has not been used or is disabled {token}")
        return httpx.Response(200, json={"audioContent": base64.b64encode(b"mp3!").decode()})

    monkeypatch.setattr(speech, "_http_transport", _mock(handler))
    gid = await _seed(session_factory, STD)
    _, c = _build(
        session_factory, tmp_path, padrino_tts_provider="gcp", padrino_tts_gcp_project="proj-1"
    )
    async with c:
        r = await c.get(f"/public/games/{gid}/speech/1")
        assert r.status_code == 200 and r.content == b"mp3!"
        assert r.headers["content-type"] == "audio/mpeg"
        assert seen[0].headers["x-goog-user-project"] == "proj-1"
        body = seen[0].content.decode()
        assert speech.GCP_SEAT_VOICES[2] in body and "MP3" in body
        mode["disabled"] = True
        with caplog.at_level(logging.DEBUG):
            r = await c.get(f"/public/games/{gid}/speech/2")
    assert r.status_code == 502
    assert r.json()["detail"] == "tts_gcp_api_disabled"
    assert token not in r.text and token not in caplog.text


async def test_say_helper_uses_stdin_not_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, data: bytes | None = None) -> tuple[bytes, bytes]:
            captured["stdin"] = data
            out = Path(captured["argv"][captured["argv"].index("-o") + 1])
            out.write_bytes(WAV)
            return b"", b""

    async def fake_exec(*argv: str, **kw: Any) -> FakeProc:
        captured["argv"] = list(argv)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    evil = "-v Bad; $(rm -rf /)"
    assert await speech._run_say("Daniel", evil) == WAV
    assert captured["stdin"] == evil.encode()
    assert evil not in captured["argv"]
    assert captured["argv"][-2:] == ["-f", "-"]
