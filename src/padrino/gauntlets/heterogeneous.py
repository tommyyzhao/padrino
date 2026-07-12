"""Heterogeneous-roster adapter assembly for US-083.

A normal gauntlet clones one model across all seven seats. This module
builds a per-seat :class:`~padrino.llm.multiplex.SeatMultiplexAdapter` from
an ``agent_build_assignments`` mapping (seat ``public_player_id`` ->
:class:`~padrino.llm.adapter.AgentBuild`) so each seat runs a DISTINCT model
identity. That is the seam that turns the gauntlet into a head-to-head
benchmark: the leaderboard's per-model rating deltas then reflect
competition between models rather than self-play variance.

Each non-OpenRouter seat gets a single-host :class:`RoutingPolicy` — no
different-model fallback and no same-model alternate host. An OpenRouter seat
also keeps one canonical requested model, while its explicit provider policy
may permit router fallback within that selected model. A heterogeneous
gauntlet measures the model assigned to each seat, so silently routing a
failed Cerebras call to Z.AI (the US-079 same-model fallback) would muddy
attribution. The same-model fallback MECHANISM stays available for
single-model runs; it is simply not engaged here.

Lives in the impure ``gauntlets`` layer; pure-core never imports it.
"""

from __future__ import annotations

from collections.abc import Mapping

from padrino.core.enums import Role
from padrino.llm.adapter import AgentBuild, LlmAdapter, RoutingPolicy
from padrino.llm.litellm_adapter import LiteLlmAdapter
from padrino.llm.multiplex import SeatMultiplexAdapter
from padrino.llm.prompts import canonical_prompts_by_role
from padrino.llm.secrets import SecretResolutionError
from padrino.settings import Settings


def build_heterogeneous_adapter(
    agent_build_assignments: Mapping[str, AgentBuild],
    *,
    settings: Settings,
    timeout_s: float | None = None,
    system_prompts_by_role: Mapping[Role, str] | None = None,
    auth_secrets_by_seat: Mapping[str, str] | None = None,
) -> SeatMultiplexAdapter:
    """Build a per-seat multiplex adapter from a seat -> ``AgentBuild`` mapping.

    Each projected :class:`AgentBuild` supplies its provider name, opaque
    credential reference, and optional OpenAI-compatible base URL. Credentials
    are resolved eagerly inside each :class:`LiteLlmAdapter` constructor, so a
    missing or unsafe reference fails before a game can start rather than on
    the first real call. The construction error deliberately omits the secret
    reference and its resolved value.
    """
    if not agent_build_assignments:
        raise ValueError("agent_build_assignments must be non-empty")
    prompts = (
        system_prompts_by_role
        if system_prompts_by_role is not None
        else canonical_prompts_by_role()
    )
    adapters: dict[str, LlmAdapter] = {}
    for seat, build in agent_build_assignments.items():
        is_openrouter = build.provider.casefold() == "openrouter"
        runtime_secret = None if auth_secrets_by_seat is None else auth_secrets_by_seat.get(seat)
        auth_secret_ref = None if runtime_secret is not None else build.auth_secret_ref
        if runtime_secret is None and auth_secret_ref is None and is_openrouter:
            auth_secret_ref = settings.padrino_openrouter_inference_key_ref
        if runtime_secret is None and auth_secret_ref is None:
            raise ValueError(
                f"seat {seat!r}: provider {build.provider!r} credential configuration is missing"
            )
        resolved_timeout = (
            timeout_s
            if timeout_s is not None
            else (
                settings.padrino_openrouter_timeout_seconds
                if is_openrouter
                else float(settings.padrino_llm_timeout_seconds)
            )
        )
        routing_policy = (
            settings.build_openrouter_routing_policy(build.model_id)
            if is_openrouter
            else RoutingPolicy(primary_model=build.model_id, fallback_model=None)
        )
        api_base = (
            build.base_url
            if build.base_url is not None
            else (settings.padrino_openrouter_base_url if is_openrouter else None)
        )
        try:
            adapters[seat] = LiteLlmAdapter(
                routing_policy=routing_policy,
                agent_build=build,
                timeout_s=resolved_timeout,
                auth_secret_ref=auth_secret_ref,
                api_key=runtime_secret,
                api_base=api_base,
                system_prompts_by_role=prompts,
            )
        except SecretResolutionError:
            raise ValueError(
                f"seat {seat!r}: provider {build.provider!r} credential configuration is invalid"
            ) from None
    return SeatMultiplexAdapter(adapters)


__all__ = ["build_heterogeneous_adapter"]
