"""One-command deployment bootstrap (US-058).

``padrino bootstrap`` walks a fresh Padrino installation from an empty database
to a ready-to-serve state. Each step is idempotent so the same command can be
re-run after partial failures or configuration changes:

1. Run ``alembic upgrade head`` to bring schema up to date.
2. Seed canonical mini7_v1 prompts if any of the four role-family rows are
   missing (migration 0005 seeds them on a fresh DB; this step is a safety
   net for databases that were rolled forward from an older snapshot or had
   the canonical rows manually deleted).
3. Seed the default mini7_v1 League if no league with that name exists.
4. Optionally mint one admin API key (``--with-admin-key``); the raw key is
   returned in the result exactly once and never stored.
5. Optionally register providers from a YAML file (``--providers``).

The module is impure (DB, file I/O, alembic) and lives outside ``padrino.core``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import structlog
import yaml
from alembic import command
from alembic.config import Config as AlembicConfig
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.auth import SCOPE_ADMIN, generate_raw_key
from padrino.core.rulesets import mini7_v1
from padrino.db.base import create_engine, create_session_factory
from padrino.db.models import League, PromptVersion
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.db.repositories import leagues as leagues_repo
from padrino.db.repositories import providers as providers_repo
from padrino.llm.prompts import (
    CANONICAL_RESPONSE_SCHEMA,
    CANONICAL_VERSION,
    iter_canonical_prompts,
)
from padrino.llm.secrets import SecretResolutionError, resolve_secret

_LOG = structlog.get_logger(__name__)

DEFAULT_LEAGUE_NAME: Final[str] = "Default League"
ADMIN_KEY_LABEL: Final[str] = "bootstrap-admin"

STEP_MIGRATIONS = "migrations"
STEP_CANONICAL_PROMPTS = "canonical_prompts"
STEP_DEFAULT_LEAGUE = "default_league"
STEP_ADMIN_KEY = "admin_key"
STEP_PROVIDERS = "providers"


class BootstrapError(RuntimeError):
    """Raised when a bootstrap step fails fatally."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"{step}: {message}")
        self.step = step
        self.message = message


class ProviderSpec(BaseModel):
    """One provider entry in the bootstrap YAML."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    auth_secret_ref: str = Field(min_length=1)
    base_url: str | None = None
    default_model: str | None = None
    timeout_s: float | None = Field(default=None, gt=0)


class ProvidersFile(BaseModel):
    """Top-level shape of the ``--providers`` YAML file."""

    model_config = ConfigDict(extra="forbid")

    providers: list[ProviderSpec]


@dataclass(frozen=True)
class StepReport:
    name: str
    status: str  # "ok" | "skipped" | "failed"
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BootstrapResult:
    succeeded: bool
    steps: tuple[StepReport, ...]
    failed_step: str | None = None
    failure_message: str | None = None
    admin_raw_key: str | None = None


def _alembic_config(db_url: str) -> AlembicConfig:
    cfg = AlembicConfig()
    # The migrations directory ships inside the installed package; use its
    # filesystem path rather than rely on a co-located ``alembic.ini``.
    migrations_pkg = Path(__file__).parent / "db" / "migrations"
    cfg.set_main_option("script_location", str(migrations_pkg))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _run_migrations(db_url: str) -> StepReport:
    """Run ``alembic upgrade head`` synchronously."""
    previous = os.environ.get("PADRINO_DB_URL")
    os.environ["PADRINO_DB_URL"] = db_url
    try:
        cfg = _alembic_config(db_url)
        command.upgrade(cfg, "head")
    except Exception as exc:  # pragma: no cover - alembic surfaces opaque errors
        raise BootstrapError(STEP_MIGRATIONS, str(exc)) from exc
    finally:
        if previous is None:
            os.environ.pop("PADRINO_DB_URL", None)
        else:
            os.environ["PADRINO_DB_URL"] = previous
    _LOG.info("bootstrap.step.ok", step=STEP_MIGRATIONS)
    return StepReport(name=STEP_MIGRATIONS, status="ok", detail={"db_url": db_url})


async def _seed_canonical_prompts(session: AsyncSession) -> StepReport:
    existing = (
        (
            await session.execute(
                select(PromptVersion.developer_prompt).where(
                    PromptVersion.version == CANONICAL_VERSION,
                    PromptVersion.ruleset_id == mini7_v1.RULESET_ID,
                )
            )
        )
        .scalars()
        .all()
    )
    have = set(existing)
    inserted: list[str] = []
    for template in iter_canonical_prompts(mini7_v1.RULESET_ID):
        if template.role_family.value in have:
            continue
        obj = PromptVersion(
            ruleset_id=template.ruleset_id,
            version=template.version,
            system_prompt=template.system_prompt,
            developer_prompt=template.role_family.value,
            response_schema=CANONICAL_RESPONSE_SCHEMA,
            prompt_hash=template.prompt_hash,
        )
        session.add(obj)
        inserted.append(template.role_family.value)
    await session.flush()
    status = "ok" if inserted else "skipped"
    _LOG.info(
        "bootstrap.step.ok" if inserted else "bootstrap.step.skipped",
        step=STEP_CANONICAL_PROMPTS,
        inserted=inserted,
    )
    return StepReport(
        name=STEP_CANONICAL_PROMPTS,
        status=status,
        detail={"inserted": inserted},
    )


async def _seed_default_league(session: AsyncSession) -> StepReport:
    stmt = select(League).where(
        League.name == DEFAULT_LEAGUE_NAME,
        League.ruleset_id == mini7_v1.RULESET_ID,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        _LOG.info(
            "bootstrap.step.skipped",
            step=STEP_DEFAULT_LEAGUE,
            league_id=str(existing.id),
        )
        return StepReport(
            name=STEP_DEFAULT_LEAGUE,
            status="skipped",
            detail={"league_id": str(existing.id)},
        )
    league = await leagues_repo.create(
        session,
        name=DEFAULT_LEAGUE_NAME,
        ruleset_id=mini7_v1.RULESET_ID,
        ranked=True,
    )
    _LOG.info(
        "bootstrap.step.ok",
        step=STEP_DEFAULT_LEAGUE,
        league_id=str(league.id),
    )
    return StepReport(
        name=STEP_DEFAULT_LEAGUE,
        status="ok",
        detail={"league_id": str(league.id)},
    )


async def _create_admin_key(session: AsyncSession) -> tuple[StepReport, str]:
    raw_key = generate_raw_key()
    record = await api_keys_repo.create(
        session,
        raw_key=raw_key,
        scopes=[SCOPE_ADMIN],
        label=ADMIN_KEY_LABEL,
    )
    _LOG.info(
        "bootstrap.step.ok",
        step=STEP_ADMIN_KEY,
        api_key_id=str(record.id),
        key_prefix=record.key_prefix,
    )
    report = StepReport(
        name=STEP_ADMIN_KEY,
        status="ok",
        detail={"api_key_id": str(record.id), "key_prefix": record.key_prefix},
    )
    return report, raw_key


def _load_providers_file(path: Path) -> ProvidersFile:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BootstrapError(STEP_PROVIDERS, f"cannot read {path}: {exc}") from exc
    try:
        data: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise BootstrapError(STEP_PROVIDERS, f"invalid yaml in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BootstrapError(
            STEP_PROVIDERS,
            f"top-level YAML in {path} must be a mapping with a 'providers' key",
        )
    try:
        return ProvidersFile.model_validate(data)
    except ValidationError as exc:
        raise BootstrapError(STEP_PROVIDERS, f"invalid providers file: {exc}") from exc


async def _seed_providers(session: AsyncSession, specs: Sequence[ProviderSpec]) -> StepReport:
    inserted: list[str] = []
    skipped: list[str] = []
    for spec in specs:
        # Resolve the secret eagerly so a bad ref fails the bootstrap
        # before any provider row is persisted (matches the POST
        # /model-providers semantics from US-050).
        try:
            resolve_secret(spec.auth_secret_ref)
        except SecretResolutionError as exc:
            raise BootstrapError(
                STEP_PROVIDERS,
                f"provider {spec.name!r}: {exc}",
            ) from exc
        existing = await providers_repo.list_(session, name=spec.name)
        if existing:
            skipped.append(spec.name)
            continue
        await providers_repo.create(
            session,
            name=spec.name,
            auth_secret_ref=spec.auth_secret_ref,
            base_url=spec.base_url,
        )
        inserted.append(spec.name)
    await session.flush()
    status = "ok" if inserted else "skipped"
    _LOG.info(
        "bootstrap.step.ok" if inserted else "bootstrap.step.skipped",
        step=STEP_PROVIDERS,
        inserted=inserted,
        skipped=skipped,
    )
    return StepReport(
        name=STEP_PROVIDERS,
        status=status,
        detail={"inserted": inserted, "skipped": skipped},
    )


async def bootstrap(
    *,
    db_url: str,
    with_admin_key: bool = False,
    providers_path: Path | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> BootstrapResult:
    """Run every bootstrap step against ``db_url`` in order.

    Returns a :class:`BootstrapResult` describing each step and the raw admin
    key (when minted). The function never raises; failures are surfaced via
    ``result.succeeded=False`` plus ``result.failed_step``. The caller is
    responsible for exit-code translation.

    A pre-built ``session_factory`` may be supplied (tests pass one to share an
    engine across assertions); otherwise the function constructs one from
    ``db_url`` and disposes it before returning.
    """
    steps: list[StepReport] = []
    admin_raw_key: str | None = None
    parsed_providers: ProvidersFile | None = None

    # Step 1: alembic upgrade head. Alembic's ``env.py`` uses ``asyncio.run``
    # internally so we have to drive it from a worker thread — we're already
    # inside an event loop here.
    try:
        steps.append(await asyncio.to_thread(_run_migrations, db_url))
    except BootstrapError as exc:
        return BootstrapResult(
            succeeded=False,
            steps=tuple(steps),
            failed_step=exc.step,
            failure_message=exc.message,
        )

    # Parse the providers file before opening DB sessions so a malformed file
    # fails fast (the migration step has already succeeded, but no DB writes
    # for the providers step happen until validation is clean).
    if providers_path is not None:
        try:
            parsed_providers = _load_providers_file(providers_path)
        except BootstrapError as exc:
            return BootstrapResult(
                succeeded=False,
                steps=tuple(steps),
                failed_step=exc.step,
                failure_message=exc.message,
            )

    owns_engine = session_factory is None
    engine = None
    if session_factory is None:
        engine = create_engine(db_url)
        session_factory = create_session_factory(engine)

    try:
        try:
            async with session_factory() as session, session.begin():
                steps.append(await _seed_canonical_prompts(session))
                steps.append(await _seed_default_league(session))
                if with_admin_key:
                    admin_step, admin_raw_key = await _create_admin_key(session)
                    steps.append(admin_step)
                if parsed_providers is not None:
                    steps.append(await _seed_providers(session, parsed_providers.providers))
        except BootstrapError as exc:
            return BootstrapResult(
                succeeded=False,
                steps=tuple(steps),
                failed_step=exc.step,
                failure_message=exc.message,
            )
    finally:
        if owns_engine and engine is not None:
            await engine.dispose()

    return BootstrapResult(
        succeeded=True,
        steps=tuple(steps),
        admin_raw_key=admin_raw_key,
    )


__all__ = [
    "ADMIN_KEY_LABEL",
    "DEFAULT_LEAGUE_NAME",
    "STEP_ADMIN_KEY",
    "STEP_CANONICAL_PROMPTS",
    "STEP_DEFAULT_LEAGUE",
    "STEP_MIGRATIONS",
    "STEP_PROVIDERS",
    "BootstrapError",
    "BootstrapResult",
    "ProviderSpec",
    "ProvidersFile",
    "StepReport",
    "bootstrap",
]
