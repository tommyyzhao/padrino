"""Heterogeneous-roster adapter assembly for US-083.

A normal gauntlet clones one model across all seven seats. This module
builds a per-seat :class:`~padrino.llm.multiplex.SeatMultiplexAdapter` from
an ``agent_build_assignments`` mapping (seat ``public_player_id`` ->
:class:`~padrino.llm.adapter.AgentBuild`) so each seat runs a DISTINCT model
identity. That is the seam that turns the gauntlet into a head-to-head
benchmark: the leaderboard's per-model rating deltas then reflect
competition between models rather than self-play variance.

Each seat gets a single-host :class:`RoutingPolicy` — no different-model
fallback and no same-model alternate host. A heterogeneous gauntlet measures
the model assigned to each seat, so silently routing a failed Cerebras call
to Z.AI (the US-079 same-model fallback) would muddy attribution. The
same-model fallback MECHANISM stays available for single-model runs; it is
simply not engaged here.

Lives in the impure ``gauntlets`` layer; pure-core never imports it.
"""

from __future__ import annotations

from collections.abc import Mapping

from padrino.core.enums import Role
from padrino.llm.adapter import AgentBuild, LlmAdapter, RoutingPolicy
from padrino.llm.litellm_adapter import LiteLlmAdapter
from padrino.llm.multiplex import SeatMultiplexAdapter
from padrino.llm.prompts import canonical_prompts_by_role
from padrino.settings import Settings


def provider_endpoints(settings: Settings) -> dict[str, tuple[str, str | None]]:
    """Map a provider name to ``(auth_secret_ref, api_base)``.

    ``api_base`` is ``None`` for providers LiteLLM routes natively from a
    ``<provider>/<model>`` id (Cerebras, DeepInfra) — those resolve their
    credential from the provider-specific env var, so the adapter leaves the
    per-call ``api_key`` unset and lets LiteLLM read it. OpenAI-compatible
    custom endpoints (Xiaomi, Z.AI) carry an explicit base URL and the adapter
    passes the resolved key as ``api_key``.
    """
    return {
        "cerebras": ("env:CEREBRAS_API_KEY", None),
        "deepinfra": ("env:DEEPINFRA_API_KEY", None),
        "xiaomi": ("env:XIAOMI_API_KEY", settings.xiaomi_base_url),
        "zai": ("env:ZAI_API_KEY", settings.padrino_zai_api_base),
    }


def build_heterogeneous_adapter(
    agent_build_assignments: Mapping[str, AgentBuild],
    *,
    settings: Settings,
    timeout_s: float | None = None,
    system_prompts_by_role: Mapping[Role, str] | None = None,
) -> SeatMultiplexAdapter:
    """Build a per-seat multiplex adapter from a seat -> ``AgentBuild`` mapping.

    The provider credential and (for OpenAI-compatible endpoints) base URL are
    resolved from :func:`provider_endpoints`. Credentials are resolved eagerly
    inside each :class:`LiteLlmAdapter` constructor, so a missing key fails
    loudly here rather than silently on the first real call.
    """
    if not agent_build_assignments:
        raise ValueError("agent_build_assignments must be non-empty")
    endpoints = provider_endpoints(settings)
    prompts = (
        system_prompts_by_role
        if system_prompts_by_role is not None
        else canonical_prompts_by_role()
    )
    resolved_timeout = (
        timeout_s if timeout_s is not None else float(settings.padrino_llm_timeout_seconds)
    )
    adapters: dict[str, LlmAdapter] = {}
    for seat, build in agent_build_assignments.items():
        try:
            auth_secret_ref, api_base = endpoints[build.provider]
        except KeyError as exc:
            raise ValueError(
                f"seat {seat!r}: unknown provider {build.provider!r}; "
                f"known providers={sorted(endpoints)}"
            ) from exc
        adapters[seat] = LiteLlmAdapter(
            routing_policy=RoutingPolicy(primary_model=build.model_id, fallback_model=None),
            agent_build=build,
            timeout_s=resolved_timeout,
            auth_secret_ref=auth_secret_ref,
            api_base=api_base,
            system_prompts_by_role=prompts,
        )
    return SeatMultiplexAdapter(adapters)


__all__ = ["build_heterogeneous_adapter", "provider_endpoints"]
