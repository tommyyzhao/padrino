"""Admin CRUD routes for providers, model configs, prompts, and agent builds.

Routes registered here register the building blocks an operator needs before
gauntlets can be scheduled. The GET /agent-builds/{id} response intentionally
excludes any joined provider secret references — clients only see the
build-local configuration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.auth import require_admin, require_read
from padrino.api.deps import get_session
from padrino.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    CursorPage,
    paginate_keyset,
)
from padrino.db.models import (
    AgentBuild,
    ModelConfig,
    ModelProvider,
    PromptVersion,
)
from padrino.db.repositories import (
    agent_builds as agent_builds_repo,
)
from padrino.db.repositories import (
    model_configs as model_configs_repo,
)
from padrino.db.repositories import (
    prompt_versions as prompt_versions_repo,
)
from padrino.db.repositories import (
    providers as providers_repo,
)
from padrino.llm.secrets import SecretResolutionError, resolve_secret

router = APIRouter()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelProviderCreate(_StrictModel):
    name: str = Field(min_length=1)
    auth_secret_ref: str = Field(min_length=1)
    base_url: str | None = None


class ModelProviderResponse(BaseModel):
    id: uuid.UUID
    name: str
    base_url: str | None
    created_at: datetime


class ModelConfigCreate(_StrictModel):
    provider_id: uuid.UUID
    model_name: str = Field(min_length=1)
    default_temperature: float
    default_top_p: float
    default_max_output_tokens: int = Field(gt=0)
    supports_structured_outputs: bool
    litellm_model_id: str | None = None
    model_version: str | None = None


class ModelConfigResponse(BaseModel):
    id: uuid.UUID
    provider_id: uuid.UUID
    model_name: str
    litellm_model_id: str | None
    model_version: str | None
    default_temperature: float
    default_top_p: float
    default_max_output_tokens: int
    supports_structured_outputs: bool
    created_at: datetime


class PromptVersionCreate(_StrictModel):
    ruleset_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    system_prompt: str
    developer_prompt: str
    response_schema: dict[str, Any]
    prompt_hash: str = Field(min_length=1)


class PromptVersionResponse(BaseModel):
    id: uuid.UUID
    ruleset_id: str
    version: str
    system_prompt: str
    developer_prompt: str
    response_schema: dict[str, Any]
    prompt_hash: str
    created_at: datetime


class AgentBuildCreate(_StrictModel):
    display_name: str = Field(min_length=1)
    model_config_id: uuid.UUID
    prompt_version_id: uuid.UUID
    adapter_version: str = Field(min_length=1)
    inference_params: dict[str, Any]
    active: bool = True


class AgentBuildResponse(BaseModel):
    """Agent build view — never includes provider secret references."""

    id: uuid.UUID
    display_name: str
    model_config_id: uuid.UUID
    prompt_version_id: uuid.UUID
    adapter_version: str
    inference_params: dict[str, Any]
    active: bool
    created_at: datetime


@router.post(
    "/model-providers",
    response_model=ModelProviderResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_model_provider(
    body: ModelProviderCreate,
    session: AsyncSession = Depends(get_session),
) -> ModelProviderResponse:
    # Fail loud now rather than silently 401-ing when a game later tries to use
    # this provider with an unresolvable credential reference.
    try:
        resolve_secret(body.auth_secret_ref)
    except SecretResolutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"auth_secret_ref could not be resolved: {exc}",
        ) from exc
    obj = await providers_repo.create(
        session,
        name=body.name,
        auth_secret_ref=body.auth_secret_ref,
        base_url=body.base_url,
    )
    return ModelProviderResponse(
        id=obj.id,
        name=obj.name,
        base_url=obj.base_url,
        created_at=obj.created_at,
    )


@router.post(
    "/model-configs",
    response_model=ModelConfigResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_model_config(
    body: ModelConfigCreate,
    session: AsyncSession = Depends(get_session),
) -> ModelConfigResponse:
    provider = await providers_repo.get(session, body.provider_id)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown provider_id: {body.provider_id}",
        )
    obj = await model_configs_repo.create(
        session,
        provider_id=body.provider_id,
        model_name=body.model_name,
        default_temperature=body.default_temperature,
        default_top_p=body.default_top_p,
        default_max_output_tokens=body.default_max_output_tokens,
        supports_structured_outputs=body.supports_structured_outputs,
        litellm_model_id=body.litellm_model_id,
        model_version=body.model_version,
    )
    return ModelConfigResponse(
        id=obj.id,
        provider_id=obj.provider_id,
        model_name=obj.model_name,
        litellm_model_id=obj.litellm_model_id,
        model_version=obj.model_version,
        default_temperature=obj.default_temperature,
        default_top_p=obj.default_top_p,
        default_max_output_tokens=obj.default_max_output_tokens,
        supports_structured_outputs=obj.supports_structured_outputs,
        created_at=obj.created_at,
    )


@router.post(
    "/prompt-versions",
    response_model=PromptVersionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_prompt_version(
    body: PromptVersionCreate,
    session: AsyncSession = Depends(get_session),
) -> PromptVersionResponse:
    obj = await prompt_versions_repo.create(
        session,
        ruleset_id=body.ruleset_id,
        version=body.version,
        system_prompt=body.system_prompt,
        developer_prompt=body.developer_prompt,
        response_schema=body.response_schema,
        prompt_hash=body.prompt_hash,
    )
    return PromptVersionResponse(
        id=obj.id,
        ruleset_id=obj.ruleset_id,
        version=obj.version,
        system_prompt=obj.system_prompt,
        developer_prompt=obj.developer_prompt,
        response_schema=obj.response_schema,
        prompt_hash=obj.prompt_hash,
        created_at=obj.created_at,
    )


@router.post(
    "/agent-builds",
    response_model=AgentBuildResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_agent_build(
    body: AgentBuildCreate,
    session: AsyncSession = Depends(get_session),
) -> AgentBuildResponse:
    mc = await model_configs_repo.get(session, body.model_config_id)
    if mc is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown model_config_id: {body.model_config_id}",
        )
    pv = await prompt_versions_repo.get(session, body.prompt_version_id)
    if pv is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown prompt_version_id: {body.prompt_version_id}",
        )
    obj = await agent_builds_repo.create(
        session,
        display_name=body.display_name,
        model_config_id=body.model_config_id,
        prompt_version_id=body.prompt_version_id,
        adapter_version=body.adapter_version,
        inference_params=body.inference_params,
        active=body.active,
    )
    return AgentBuildResponse(
        id=obj.id,
        display_name=obj.display_name,
        model_config_id=obj.model_config_id,
        prompt_version_id=obj.prompt_version_id,
        adapter_version=obj.adapter_version,
        inference_params=obj.inference_params,
        active=obj.active,
        created_at=obj.created_at,
    )


class ModelProviderListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT)
    cursor: str | None = None
    name: str | None = None


@router.get(
    "/model-providers",
    response_model=CursorPage[ModelProviderResponse],
    dependencies=[Depends(require_read)],
)
async def list_model_providers(
    query: Annotated[ModelProviderListQuery, Query()],
    session: AsyncSession = Depends(get_session),
) -> CursorPage[ModelProviderResponse]:
    stmt = select(ModelProvider)
    if query.name is not None:
        stmt = stmt.where(ModelProvider.name == query.name)
    rows, next_cursor = await paginate_keyset(
        session,
        stmt,
        created_at_col=ModelProvider.created_at,
        id_col=ModelProvider.id,
        limit=query.limit,
        cursor=query.cursor,
    )
    items = [
        ModelProviderResponse(
            id=r.id,
            name=r.name,
            base_url=r.base_url,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return CursorPage[ModelProviderResponse](items=items, next_cursor=next_cursor)


class ModelConfigListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT)
    cursor: str | None = None
    provider_id: uuid.UUID | None = None
    model_name: str | None = None


@router.get(
    "/model-configs",
    response_model=CursorPage[ModelConfigResponse],
    dependencies=[Depends(require_read)],
)
async def list_model_configs(
    query: Annotated[ModelConfigListQuery, Query()],
    session: AsyncSession = Depends(get_session),
) -> CursorPage[ModelConfigResponse]:
    stmt = select(ModelConfig)
    if query.provider_id is not None:
        stmt = stmt.where(ModelConfig.provider_id == query.provider_id)
    if query.model_name is not None:
        stmt = stmt.where(ModelConfig.model_name == query.model_name)
    rows, next_cursor = await paginate_keyset(
        session,
        stmt,
        created_at_col=ModelConfig.created_at,
        id_col=ModelConfig.id,
        limit=query.limit,
        cursor=query.cursor,
    )
    items = [
        ModelConfigResponse(
            id=r.id,
            provider_id=r.provider_id,
            model_name=r.model_name,
            litellm_model_id=r.litellm_model_id,
            model_version=r.model_version,
            default_temperature=r.default_temperature,
            default_top_p=r.default_top_p,
            default_max_output_tokens=r.default_max_output_tokens,
            supports_structured_outputs=r.supports_structured_outputs,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return CursorPage[ModelConfigResponse](items=items, next_cursor=next_cursor)


class PromptVersionListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT)
    cursor: str | None = None
    ruleset_id: str | None = None


@router.get(
    "/prompt-versions",
    response_model=CursorPage[PromptVersionResponse],
    dependencies=[Depends(require_read)],
)
async def list_prompt_versions(
    query: Annotated[PromptVersionListQuery, Query()],
    session: AsyncSession = Depends(get_session),
) -> CursorPage[PromptVersionResponse]:
    stmt = select(PromptVersion)
    if query.ruleset_id is not None:
        stmt = stmt.where(PromptVersion.ruleset_id == query.ruleset_id)
    rows, next_cursor = await paginate_keyset(
        session,
        stmt,
        created_at_col=PromptVersion.created_at,
        id_col=PromptVersion.id,
        limit=query.limit,
        cursor=query.cursor,
    )
    items = [
        PromptVersionResponse(
            id=r.id,
            ruleset_id=r.ruleset_id,
            version=r.version,
            system_prompt=r.system_prompt,
            developer_prompt=r.developer_prompt,
            response_schema=r.response_schema,
            prompt_hash=r.prompt_hash,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return CursorPage[PromptVersionResponse](items=items, next_cursor=next_cursor)


class AgentBuildListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT)
    cursor: str | None = None
    active: bool | None = None
    model_config_id: uuid.UUID | None = None
    prompt_version_id: uuid.UUID | None = None


@router.get(
    "/agent-builds",
    response_model=CursorPage[AgentBuildResponse],
    dependencies=[Depends(require_read)],
)
async def list_agent_builds(
    query: Annotated[AgentBuildListQuery, Query()],
    session: AsyncSession = Depends(get_session),
) -> CursorPage[AgentBuildResponse]:
    stmt = select(AgentBuild)
    if query.active is not None:
        stmt = stmt.where(AgentBuild.active == query.active)
    if query.model_config_id is not None:
        stmt = stmt.where(AgentBuild.model_config_id == query.model_config_id)
    if query.prompt_version_id is not None:
        stmt = stmt.where(AgentBuild.prompt_version_id == query.prompt_version_id)
    rows, next_cursor = await paginate_keyset(
        session,
        stmt,
        created_at_col=AgentBuild.created_at,
        id_col=AgentBuild.id,
        limit=query.limit,
        cursor=query.cursor,
    )
    items = [
        AgentBuildResponse(
            id=r.id,
            display_name=r.display_name,
            model_config_id=r.model_config_id,
            prompt_version_id=r.prompt_version_id,
            adapter_version=r.adapter_version,
            inference_params=r.inference_params,
            active=r.active,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return CursorPage[AgentBuildResponse](items=items, next_cursor=next_cursor)


@router.get(
    "/agent-builds/{agent_build_id}",
    response_model=AgentBuildResponse,
    dependencies=[Depends(require_read)],
)
async def get_agent_build(
    agent_build_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> AgentBuildResponse:
    obj = await agent_builds_repo.get(session, agent_build_id)
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"agent_build {agent_build_id} not found",
        )
    return AgentBuildResponse(
        id=obj.id,
        display_name=obj.display_name,
        model_config_id=obj.model_config_id,
        prompt_version_id=obj.prompt_version_id,
        adapter_version=obj.adapter_version,
        inference_params=obj.inference_params,
        active=obj.active,
        created_at=obj.created_at,
    )


__all__ = [
    "AgentBuildCreate",
    "AgentBuildResponse",
    "ModelConfigCreate",
    "ModelConfigResponse",
    "ModelProviderCreate",
    "ModelProviderResponse",
    "PromptVersionCreate",
    "PromptVersionResponse",
    "router",
]
