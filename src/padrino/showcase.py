"""Unranked, anonymous localhost AI games through a local OpenAI-compatible gateway."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import urlsplit

import litellm
from sqlalchemy import event, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.bootstrap import _run_migrations
from padrino.core.enums import IdentityMode, Role
from padrino.core.rulesets import get_ruleset
from padrino.db.base import create_engine, create_session_factory
from padrino.db.game_status import GAME_STATUS_RUNNING
from padrino.db.models import AgentBuild, LlmCall, ModelConfig, ModelProvider, PromptVersion
from padrino.db.repositories import games as games_repo
from padrino.economics.human_cost_governance import PRICE_BASIS_UNPRICED
from padrino.gauntlets.heterogeneous import build_heterogeneous_adapter
from padrino.gauntlets.tournament import project_agent_build
from padrino.llm.adapter import AgentBuild as RuntimeBuild
from padrino.llm.adapter import LlmAdapter, ProviderNoContestError
from padrino.llm.prompts import (
    CANONICAL_RESPONSE_SCHEMA,
    canonical_prompts_by_role,
    iter_canonical_prompts,
)
from padrino.llm.secrets import SecretResolutionError, resolve_secret
from padrino.public.broadcast_index import mark_hidden, mark_live, mark_recent
from padrino.runner.game_runner import GameConfig, GamePersistence, run_game
from padrino.settings import Settings

DEFAULT_DB_URL = "sqlite+aiosqlite:///./padrino-showcase.db"
DEFAULT_BASE_URL = "http://127.0.0.1:4000/v1"
DEFAULT_KEY_REF = "env:PADRINO_GATEWAY_API_KEY"
ADAPTER_VERSION = "showcase-v2"
DEFAULT_LANGUAGE = "English"
DEFAULT_MAX_TOKENS = 4096
# Some gateways (e.g. 9router token-saver) inject style prompts that can flip reply language.
GATEWAY_HEADERS = {"x-9router-token-saver": "off"}
PREFLIGHT_PROMPT = "Preflight check: reply with the single word OK."
AdapterFactory = Callable[[Mapping[str, RuntimeBuild]], LlmAdapter]
Preflight = Callable[[str], Awaitable[None]]


class ShowcaseError(RuntimeError):
    """Safe operator-facing failure; never contains provider exception text."""


class ShowcaseSummary(TypedDict):
    game_id: str
    winner: str | None
    calls: int
    # Sum of real (provider-reported or listed-price) costs only; never an estimate.
    cost: float | None
    # Calls with tokens but no known price; ``cost`` excludes them.
    unpriced_calls: int


def _validate(models: Sequence[str], db_url: str, base_url: str, key_ref: str) -> None:
    ruleset = get_ruleset("mini7_v1")
    if len(models) != ruleset.PLAYER_COUNT:
        raise ShowcaseError(f"mini7_v1 requires exactly {ruleset.PLAYER_COUNT} model slugs")
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./:-]+", slug) for slug in models):
        raise ShowcaseError(
            "Models must be provider/model slugs, without whitespace or credentials"
        )
    # ponytail: moderation is skipped for local SQLite only; before any public
    # deployment run public.moderation.is_broadcastable before publication.
    try:
        url = make_url(db_url)
    except (ArgumentError, ValueError):
        raise ShowcaseError("--db-url must be a local SQLite async URL") from None
    if url.drivername != "sqlite+aiosqlite" or url.host is not None:
        raise ShowcaseError("--db-url must use local SQLite (sqlite+aiosqlite:///...)")
    try:
        parsed = urlsplit(base_url)
        _ = parsed.port
    except ValueError:
        raise ShowcaseError("--base-url must be a valid HTTP URL") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ShowcaseError(
            "--base-url must be an HTTP URL without credentials, query, or fragment"
        )
    if not (
        re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", key_ref)
        or (key_ref.startswith("file:") and Path(key_ref[5:]).is_absolute())
    ):
        raise ShowcaseError(
            "--key-ref must be env:VARIABLE or file:/absolute/path, never a raw key"
        )


def _language_instruction(language: str) -> str:
    return (
        f"\n\nShowcase language rule: write every public_message and private_message "
        f"in {language}, regardless of the language of other players or of this prompt."
    )


def _showcase_prompts(language: str | None) -> Mapping[Role, str]:
    """Canonical prompts plus an optional showcase-only language rule (hashes untouched)."""
    prompts = canonical_prompts_by_role("mini7_v1")
    if language is None:
        return prompts
    suffix = _language_instruction(language)
    return {role: text + suffix for role, text in prompts.items()}


async def _preflight_call(slug: str, *, base_url: str, api_key: str, timeout_s: float) -> None:
    """One bounded call on the same litellm/gateway route games use; any answer is success.

    Reasoning models think before output, so max_tokens matches the game build and
    empty content is not a failure; only an error or exceeding ``timeout_s`` is.
    """
    await asyncio.wait_for(
        litellm.acompletion(
            model="openai/" + slug,
            messages=[
                {"role": "system", "content": "You are a connectivity check."},
                {"role": "user", "content": PREFLIGHT_PROMPT},
            ],
            max_tokens=DEFAULT_MAX_TOKENS,
            timeout=timeout_s,
            api_base=base_url,
            api_key=api_key,
            extra_headers=GATEWAY_HEADERS,
        ),
        timeout_s,
    )


async def _preflight(models: Sequence[str], call: Preflight) -> None:
    """Probe each distinct slug concurrently; name every failing slug, never the cause text."""
    slugs = list(dict.fromkeys(models))
    results = await asyncio.gather(*(call(slug) for slug in slugs), return_exceptions=True)
    failed = [
        f"{slug} ({'timeout' if isinstance(r, TimeoutError | litellm.exceptions.Timeout) else 'error'})"
        for slug, r in zip(slugs, results, strict=True)
        if isinstance(r, BaseException)
    ]
    if failed:
        raise ShowcaseError(f"Preflight failed for {', '.join(failed)}; no game was created")


def _sqlite_pragmas(connection: Any, _record: Any) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
    finally:
        cursor.close()


async def _seed_builds(
    session: AsyncSession,
    models: Sequence[str],
    *,
    base_url: str,
    key_ref: str,
    adapter_version: str = ADAPTER_VERSION,
) -> dict[str, uuid.UUID]:
    provider = (
        await session.execute(
            select(ModelProvider)
            .where(ModelProvider.name == "gateway")
            .order_by(ModelProvider.created_at, ModelProvider.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if provider is None:
        provider = ModelProvider(name="gateway", base_url=base_url, auth_secret_ref=key_ref)
        session.add(provider)
        await session.flush()
    else:
        provider.base_url = base_url
        provider.auth_secret_ref = key_ref
    prompt: PromptVersion | None = None
    for template in iter_canonical_prompts("mini7_v1"):
        row = (
            await session.execute(
                select(PromptVersion).where(PromptVersion.prompt_hash == template.prompt_hash)
            )
        ).scalar_one_or_none()
        if row is None:
            row = PromptVersion(
                ruleset_id=template.ruleset_id,
                version=template.version,
                system_prompt=template.system_prompt,
                developer_prompt=template.role_family.value,
                response_schema=CANONICAL_RESPONSE_SCHEMA,
                prompt_hash=template.prompt_hash,
            )
            session.add(row)
            await session.flush()
        if template.role_family.value == "VANILLA_TOWN":
            prompt = row
    assert prompt is not None
    builds: dict[str, uuid.UUID] = {}
    for slug in dict.fromkeys(models):
        model = (
            await session.execute(
                select(ModelConfig)
                .where(
                    ModelConfig.provider_id == provider.id,
                    ModelConfig.model_name == slug,
                    ModelConfig.litellm_model_id == "openai/" + slug,
                )
                .order_by(ModelConfig.created_at, ModelConfig.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if model is None:
            ruleset = get_ruleset("mini7_v1")
            model = ModelConfig(
                provider_id=provider.id,
                model_name=slug,
                litellm_model_id="openai/" + slug,
                default_temperature=ruleset.TEMPERATURE,
                default_top_p=ruleset.TOP_P,
                default_max_output_tokens=DEFAULT_MAX_TOKENS,
                supports_structured_outputs=True,
            )
            session.add(model)
            await session.flush()
        build = (
            await session.execute(
                select(AgentBuild)
                .where(
                    AgentBuild.model_config_id == model.id,
                    AgentBuild.prompt_version_id == prompt.id,
                    AgentBuild.adapter_version == adapter_version,
                )
                .order_by(AgentBuild.created_at, AgentBuild.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if build is None:
            build = AgentBuild(
                display_name=slug,
                model_config_id=model.id,
                prompt_version_id=prompt.id,
                adapter_version=adapter_version,
                inference_params={"max_tokens": model.default_max_output_tokens},
                active=True,
            )
            session.add(build)
            await session.flush()
        builds[slug] = build.id
    return {f"P{index + 1:02d}": builds[slug] for index, slug in enumerate(models)}


async def run_showcase(
    models: Sequence[str],
    *,
    db_url: str = DEFAULT_DB_URL,
    base_url: str = DEFAULT_BASE_URL,
    seed: str | None = None,
    key_ref: str = DEFAULT_KEY_REF,
    adapter_factory: AdapterFactory | None = None,
    language: str | None = DEFAULT_LANGUAGE,
    preflight: Preflight | None = None,
) -> ShowcaseSummary:
    """Preflight models, seed idempotently, publish before inference, persist one game.

    ``language`` adds a showcase-only chat-language rule to the system prompts (None
    disables it); canonical prompt rows/hashes are untouched. ``preflight`` defaults to a
    real bounded call per slug unless ``adapter_factory`` is injected (tests).
    """
    _validate(models, db_url, base_url, key_ref)
    if language is not None and not re.fullmatch(r"[A-Za-z][A-Za-z -]{0,31}", language):
        raise ShowcaseError("--language must be a plain language name such as English")
    settings = Settings()
    if preflight is None and adapter_factory is None:
        try:
            api_key = resolve_secret(key_ref)
        except SecretResolutionError:
            raise ShowcaseError("Could not resolve the --key-ref credential") from None
        timeout_s = float(settings.padrino_llm_timeout_seconds)

        async def preflight(slug: str) -> None:
            await _preflight_call(slug, base_url=base_url, api_key=api_key, timeout_s=timeout_s)

    if preflight is not None:
        await _preflight(models, preflight)
    adapter_version = ADAPTER_VERSION if language is None else f"{ADAPTER_VERSION}+lang={language}"
    game_seed = seed if seed is not None else uuid.uuid4().hex
    try:
        await asyncio.to_thread(_run_migrations, db_url)
    except Exception:
        raise ShowcaseError("Showcase database migration failed") from None
    engine = create_engine(db_url)
    event.listen(engine.sync_engine, "connect", _sqlite_pragmas)
    session_factory = create_session_factory(engine)
    game_id: uuid.UUID | None = None
    try:
        async with session_factory() as session, session.begin():
            builds = await _seed_builds(
                session,
                models,
                base_url=base_url,
                key_ref=key_ref,
                adapter_version=adapter_version,
            )
            game = await games_repo.create(
                session, ruleset_id="mini7_v1", game_seed=game_seed, status=GAME_STATUS_RUNNING
            )
            game_id = game.id
            game.identity_mode = IdentityMode.ANONYMOUS.value
            game.started_at = datetime.now(UTC)
            game.is_broadcastable = True
            await mark_live(session, game_id)
            assignments = {
                seat: await project_agent_build(session, build_id)
                for seat, build_id in builds.items()
            }
        watch_url = f"http://localhost:5173/watch/{game_id}"
        print(f"{watch_url}?tail=1&narrated=1", flush=True)
        adapter = (
            adapter_factory(assignments)
            if adapter_factory is not None
            else build_heterogeneous_adapter(
                assignments,
                settings=settings,
                system_prompts_by_role=_showcase_prompts(language),
                extra_headers=GATEWAY_HEADERS,
            )
        )
        # ponytail: no spend cap for the local showcase; add admission here and
        # a per-call budget guard when bounded recording spend is required.
        outcome = await run_game(
            GameConfig(
                game_id=str(game_id),
                game_seed=game_seed,
                ruleset_id="mini7_v1",
                timeout_s=float(settings.padrino_llm_timeout_seconds),
            ),
            adapter,
            ranked=False,
            persistence=GamePersistence(
                session_factory=session_factory,
                game_id=game_id,
                agent_builds=builds,
                league_id=None,
                ranked=False,
                settings=settings,
                db_write_lock=asyncio.Lock(),
            ),
        )
        async with session_factory() as session, session.begin():
            await mark_recent(session, game_id)
            calls, cost, unpriced = (
                await session.execute(
                    select(
                        func.count(LlmCall.id),
                        func.sum(LlmCall.cost_usd),
                        func.count(LlmCall.id).filter(LlmCall.price_basis == PRICE_BASIS_UNPRICED),
                    ).where(LlmCall.game_id == game_id)
                )
            ).one()
        summary: ShowcaseSummary = {
            "game_id": str(game_id),
            "winner": outcome.final_state.terminal_result,
            "calls": calls,
            "cost": cost,
            "unpriced_calls": unpriced,
        }
        print(f"{watch_url}?narrated=1", flush=True)
        print(json.dumps(summary, sort_keys=True), flush=True)
        return summary
    except Exception as exc:
        # Name the unresponsive seat number only; never the model or provider behind it.
        unresponsive = exc.seat if isinstance(exc, ProviderNoContestError) else None
        detail = (
            f"seat {unresponsive} stopped responding"
            if unresponsive is not None
            else "Showcase execution failed"
        )
        if game_id is not None:
            async with session_factory() as session, session.begin():
                failed_game = await games_repo.mark_failed(
                    session,
                    game_id,
                    completed_at=datetime.now(UTC),
                    last_error=detail,
                    last_error_kind="seat_unresponsive"
                    if unresponsive is not None
                    else "showcase_failed",
                )
                if failed_game is not None:
                    failed_game.is_broadcastable = False
                    await mark_hidden(session, game_id)
        raise ShowcaseError(f"{detail}; committed events retained in the local database") from None
    finally:
        await engine.dispose()
