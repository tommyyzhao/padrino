"""Local text-to-speech for public game frames (US-409).

``GET /public/games/{game_id}/speech/{sequence}?cue=chat|phase`` returns audio
for one committed PUBLIC frame. Text and voice come only from the server:
chat text and eliminations/winner from the identity-stripped public
projection, voices from fixed per-seat tables indexed by the seat number
(never by model, provider, role, or faction). Off by default; loopback only;
the broadcast gate of ``/public/games/{id}/live`` is re-run on every request,
cache hits included.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import logging
import os
import re
import tempfile
import uuid
import weakref
from pathlib import Path
from typing import Any, Final, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.deps import get_session
from padrino.db.models import Game, GameEvent
from padrino.llm.secrets import SecretResolutionError, resolve_secret
from padrino.public.broadcast_index import BroadcastState
from padrino.public.projection import to_public_event_v1
from padrino.settings import get_settings

router = APIRouter()
_log = logging.getLogger(__name__)

MAX_TEXT_CHARS: Final[int] = 1500
TIMEOUT_S: Final[float] = 30.0

# Seat voices are indexed by seat number (P01 -> index 0). Never derive a voice
# from model, provider, role, or faction: a voice must not leak identity.
SAY_SEAT_VOICES: Final[tuple[str, ...]] = (
    "Daniel",
    "Karen",
    "Moira",
    "Kathy",
    "Fred",
    "Flo (English (UK))",
    "Junior",
)
SAY_NARRATOR_VOICE: Final[str] = "Grandpa (English (US))"
GCP_SEAT_VOICES: Final[tuple[str, ...]] = tuple(
    f"en-US-Chirp3-HD-{n}" for n in ("Aoede", "Charon", "Fenrir", "Kore", "Leda", "Orus", "Puck")
)
GCP_NARRATOR_VOICE: Final[str] = "en-US-Chirp3-HD-Zephyr"
# ElevenLabs premade voice ids.
ELEVENLABS_SEAT_VOICES: Final[tuple[str, ...]] = (
    "21m00Tcm4TlvDq8ikWAM",  # Rachel
    "ErXwobaYiN019PkySvjV",  # Antoni
    "AZnzlk1XvdvUeBnXmlld",  # Domi
    "TxGEqnHWrfWFTfGW9XjX",  # Josh
    "VR6AewLTigWG4xSOukaG",  # Arnold
    "pNInz6obpgDQGcFmaJgB",  # Adam
    "yoZ06aMxZJJ28mfd3POQ",  # Sam
)
ELEVENLABS_NARRATOR_VOICE: Final[str] = "GBv7mTt0atIp3Br8iCZE"  # Thomas

_VOICES: Final[dict[str, tuple[tuple[str, ...], str]]] = {
    "say": (SAY_SEAT_VOICES, SAY_NARRATOR_VOICE),
    "gcp": (GCP_SEAT_VOICES, GCP_NARRATOR_VOICE),
    "elevenlabs": (ELEVENLABS_SEAT_VOICES, ELEVENLABS_NARRATOR_VOICE),
}

_SEAT_RE = re.compile(r"^P(\d{1,3})$")
_PHASE_RE = re.compile(r"^(DAY|NIGHT)_(\d+)")

#: Test seam: transport for gcp/elevenlabs HTTP calls.
_http_transport: httpx.AsyncBaseTransport | None = None

#: One lock per cache key; weak so idle keys are garbage-collected.
_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


class SpeechError(Exception):
    """Provider failure. ``code`` is a fixed, secret-free string."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# Text + voice selection (pure)
# ---------------------------------------------------------------------------


def _seat_label(value: Any) -> str:
    return value if isinstance(value, str) and _SEAT_RE.match(value) else "A player"


def phase_text(phase: str) -> str:
    m = _PHASE_RE.match(phase)
    if m is None:
        return "A new phase begins."
    day = int(m.group(2))
    if m.group(1) == "NIGHT":
        return f"Night {day} falls."
    return f"Day {day}, voting begins." if "VOTE" in phase else f"Day {day} begins."


def select_utterance(frame: dict[str, Any], cue: str) -> tuple[str, str, bool] | None:
    """Return ``(text, voice_kind, is_narrator)`` or None; voice_kind is the seat id."""
    etype = frame["event_type"]
    payload = frame["payload"]
    if etype == "PlayerEliminated":
        who = _seat_label(payload.get("public_player_id") or frame.get("actor_player_id"))
        return f"{who} has been eliminated.", "", True
    if etype == "GameTerminated":
        winner = str(payload.get("winner", ""))[:40]
        return f"The game is over. {winner} wins.", "", True
    if cue == "phase":
        return phase_text(frame["phase"]), "", True
    if etype == "PublicMessageSubmitted":
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return text, str(frame.get("actor_player_id") or ""), False
    return None


def pick_voice(provider: str, speaker: str, narrator: bool) -> str:
    seats, narr = _VOICES[provider]
    m = _SEAT_RE.match(speaker)
    if narrator or m is None:
        return narr
    return seats[(int(m.group(1)) - 1) % len(seats)]


# ---------------------------------------------------------------------------
# Synthesis (impure)
# ---------------------------------------------------------------------------


async def _run_say(voice: str, text: str) -> bytes:
    """Run macOS ``say``; text goes via stdin, never argv."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.wav"
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", voice, "-o", str(out),
            "--file-format=WAVE", "--data-format=LEI16@22050", "-f", "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        try:
            await asyncio.wait_for(proc.communicate(text.encode()), TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise SpeechError("tts_timeout") from None
        if proc.returncode != 0 or not out.exists():
            raise SpeechError("tts_say_failed")
        return out.read_bytes()


async def _gcloud_token() -> str:
    """ADC access token via gcloud; held in memory only, never logged."""
    proc = await asyncio.create_subprocess_exec(
        "gcloud", "auth", "application-default", "print-access-token",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise SpeechError("tts_timeout") from None
    token = out.decode().strip()
    if proc.returncode != 0 or not token:
        raise SpeechError("tts_gcp_auth_unavailable")
    return token


async def _post(url: str, headers: dict[str, str], body: dict[str, Any]) -> httpx.Response:
    try:
        async with httpx.AsyncClient(transport=_http_transport, timeout=TIMEOUT_S) as client:
            return await client.post(url, headers=headers, json=body)
    except httpx.TimeoutException:
        raise SpeechError("tts_timeout") from None
    except httpx.HTTPError:
        raise SpeechError("tts_provider_failed") from None


async def _synth_gcp(voice: str, text: str, cfg: Any) -> bytes:
    if not cfg.padrino_tts_gcp_project:
        raise SpeechError("tts_gcp_project_unset")
    token = await _gcloud_token()
    resp = await _post(
        "https://texttospeech.googleapis.com/v1/text:synthesize",
        {"Authorization": f"Bearer {token}", "x-goog-user-project": cfg.padrino_tts_gcp_project},
        {
            "input": {"text": text},
            "voice": {"languageCode": "en-US", "name": voice},
            "audioConfig": {"audioEncoding": "MP3"},
        },
    )
    if resp.status_code == 403 and "disabled" in resp.text.lower():
        raise SpeechError("tts_gcp_api_disabled")
    if resp.status_code != 200:
        raise SpeechError("tts_provider_failed")
    try:
        return base64.b64decode(resp.json()["audioContent"], validate=True)
    except (ValueError, KeyError, TypeError):
        raise SpeechError("tts_provider_failed") from None


async def _synth_elevenlabs(voice: str, text: str, cfg: Any) -> bytes:
    try:
        key = resolve_secret(cfg.padrino_tts_elevenlabs_key_ref)
    except SecretResolutionError:
        raise SpeechError("tts_elevenlabs_key_unavailable") from None
    resp = await _post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
        {"xi-api-key": key, "Accept": "audio/mpeg"},
        {"text": text},
    )
    if resp.status_code != 200 or not resp.content:
        raise SpeechError("tts_provider_failed")
    return resp.content


async def synthesize(provider: str, voice: str, text: str, cfg: Any) -> bytes:
    """Plain branch on provider. No retries."""
    if provider == "say":
        return await _run_say(voice, text)
    if provider == "gcp":
        return await _synth_gcp(voice, text, cfg)
    return await _synth_elevenlabs(voice, text, cfg)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _cache_path(cfg: Any, provider: str, voice: str, text: str) -> Path:
    digest = hashlib.sha256(f"{provider}|{voice}|{text}".encode()).hexdigest()
    ext = "wav" if provider == "say" else "mp3"
    return Path(cfg.padrino_tts_cache_dir).expanduser() / f"{digest}.{ext}"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def _is_loopback(host: str | None) -> bool:
    try:
        return host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@router.get("/public/games/{game_id}/speech/{sequence}")
async def public_game_speech(
    game_id: uuid.UUID,
    sequence: int,
    request: Request,
    cue: Literal["chat", "phase"] = Query(default="chat"),
    session: AsyncSession = Depends(get_session),
) -> Response:
    cfg: Any = getattr(request.app.state, "auth_settings", None) or get_settings()
    provider: str | None = cfg.padrino_tts_provider
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")

    # Loopback only. A forwarded client address must also be loopback so a
    # same-host reverse proxy cannot expose this endpoint.
    xff = request.headers.get("X-Forwarded-For")
    if not _is_loopback(request.client.host if request.client else None) or (
        xff is not None and not _is_loopback(xff.split(",")[0].strip())
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="loopback_only")

    # Same broadcast gate as /public/games/{id}/live; runs on every request.
    game = await session.get(Game, game_id)
    if (
        game is None
        or not game.is_broadcastable
        or game.broadcast_state not in (BroadcastState.LIVE.value, BroadcastState.RECENT.value)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")

    row = (
        await session.execute(
            select(GameEvent).where(GameEvent.game_id == game_id, GameEvent.sequence == sequence)
        )
    ).scalar_one_or_none()
    frame = (
        None
        if row is None
        else to_public_event_v1(
            {
                "sequence": row.sequence,
                "event_type": row.event_type,
                "phase": row.phase,
                "visibility": row.visibility,
                "actor_player_id": row.actor_player_id,
                "payload": dict(row.payload) if row.payload else {},
                "prev_event_hash": row.prev_event_hash,
                "event_hash": row.event_hash,
            }
        )
    )
    utterance = None if frame is None else select_utterance(frame, cue)
    if utterance is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")

    text, speaker, narrator = utterance
    text = text[:MAX_TEXT_CHARS]
    voice = pick_voice(provider, speaker, narrator)
    path = _cache_path(cfg, provider, voice, text)

    lock = _locks.get(path.name)
    if lock is None:
        lock = _locks[path.name] = asyncio.Lock()
    async with lock:
        try:
            audio = path.read_bytes()
        except OSError:
            try:
                audio = await synthesize(provider, voice, text, cfg)
                _write_atomic(path, audio)
            except SpeechError as exc:
                _log.warning("tts synthesis failed: %s", exc.code)
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=exc.code) from None
            except OSError:
                _log.warning("tts cache write failed")
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY, detail="tts_cache_unavailable"
                ) from None

    return Response(
        content=audio,
        media_type="audio/wav" if provider == "say" else "audio/mpeg",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )
