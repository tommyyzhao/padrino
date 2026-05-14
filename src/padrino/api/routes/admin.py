"""Admin CRUD routes for providers, model configs, prompts, and agent builds.

Routes registered here register the building blocks an operator needs before
gauntlets can be scheduled. The GET /agent-builds/{id} response intentionally
excludes any joined provider secret references — clients only see the
build-local configuration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.deps import get_session
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
    model_version: str | None = None


class ModelConfigResponse(BaseModel):
    id: uuid.UUID
    provider_id: uuid.UUID
    model_name: str
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
)
async def create_model_provider(
    body: ModelProviderCreate,
    session: AsyncSession = Depends(get_session),
) -> ModelProviderResponse:
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
        model_version=body.model_version,
    )
    return ModelConfigResponse(
        id=obj.id,
        provider_id=obj.provider_id,
        model_name=obj.model_name,
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


@router.get("/agent-builds/{agent_build_id}", response_model=AgentBuildResponse)
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
