"""Durable benchmark-game rehydration from persisted event rows.

Benchmark games do not have a ``human_game_runtime`` snapshot row. Their only
authoritative resume source is the hash-chained ``game_events`` log, so this
module rebuilds :class:`padrino.runner.game_runner.GameResume` by replaying the
persisted rows through the shared durability helper used by the human lane.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.observations import format_phase_id
from padrino.db.repositories import events as events_repo
from padrino.runner.game_runner import GameResume, _resume_phase, _ruleset_for
from padrino.runner.human_durability import replay_state_from_rows


async def rehydrate_benchmark_game(
    session: AsyncSession,
    game_id: uuid.UUID,
) -> GameResume | None:
    """Rebuild resume state for one AI-only benchmark game from ``game_events``.

    Returns ``None`` when there is no useful benchmark tail to resume: no
    persisted events, only ``GameCreated``, or an already terminal replayed
    state. For every non-empty log, the hash chain is verified by
    :func:`replay_state_from_rows`; corruption raises
    :class:`padrino.core.engine.replay.ReplayHashMismatchError`.
    """
    rows = await events_repo.list_events(session, game_id)
    if not rows:
        return None

    state, event_log = replay_state_from_rows(rows)
    if len(event_log.events) == 1 and event_log.events[0].body.get("event_type") == "GameCreated":
        return None
    if state.terminal_result is not None:
        return None

    ruleset = _ruleset_for(state.ruleset_id)
    phase, _already_started = _resume_phase(state, event_log, ruleset)
    return GameResume(
        state=state,
        event_log=event_log,
        phase=format_phase_id(phase),
    )


__all__ = ["rehydrate_benchmark_game"]
