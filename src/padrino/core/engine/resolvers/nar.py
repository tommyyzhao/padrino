"""Deterministic Night Action Resolution matrix.

This module is the single source of truth for night-action interactions. The
resolver walks the same documented tier order for current and future roles:

1. roleblocks,
2. redirects,
3. kills / protects / visits,
4. investigations and visit-graph reads,
5. death reveal, including clean / forge-style reveal suppression.

Current ``mini7_v1`` / ``bench10_v1`` actions are translated into matrix
intents here so the legacy mafia-kill, doctor-protect, and detective helpers
remain byte-stable wrappers over the matrix rather than a parallel path.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from padrino.core.engine.actions import Action
from padrino.core.engine.state import GameState, Seat
from padrino.core.enums import ActionType, Faction, Role

FINDING_MAFIA: Literal["MAFIA"] = "MAFIA"
FINDING_TOWN: Literal["TOWN"] = "TOWN"

REASON_UNIQUE_PLURALITY = "unique_plurality"
REASON_TIE = "tie"
REASON_ALL_INVALID = "all_invalid"

REASON_PROTECTED = "protected"
REASON_REPEAT_VIOLATION = "REPEAT_VIOLATION"
REASON_INVALID_TARGET = "invalid_target"
REASON_NO_SUBMISSION = "no_submission"
REASON_DEAD_DOCTOR = "dead_doctor"
REASON_NO_DOCTOR = "no_doctor"

REASON_RESOLVED = "resolved"
REASON_SELF_TARGET = "self_target"
REASON_DEAD_DETECTIVE = "dead_detective"
REASON_NO_DETECTIVE = "no_detective"


class NarTier(IntEnum):
    """Formal Night Action Resolution tier order."""

    ROLEBLOCK = 1
    REDIRECT = 2
    KILL_PROTECT_VISIT = 3
    INVESTIGATION = 4
    DEATH_REVEAL = 5


TIER_ORDER: tuple[NarTier, ...] = (
    NarTier.ROLEBLOCK,
    NarTier.REDIRECT,
    NarTier.KILL_PROTECT_VISIT,
    NarTier.INVESTIGATION,
    NarTier.DEATH_REVEAL,
)


class NightActionKind(StrEnum):
    """Matrix-level night action vocabulary."""

    ROLEBLOCK = "ROLEBLOCK"
    REDIRECT = "REDIRECT"
    FACTIONAL_KILL = "FACTIONAL_KILL"
    PROTECT = "PROTECT"
    INVESTIGATE = "INVESTIGATE"
    FRAME = "FRAME"
    TRACK = "TRACK"
    WATCH = "WATCH"
    CLEAN = "CLEAN"


class MatrixEffect(StrEnum):
    """A cell value in the action-type x interaction resolution matrix."""

    UNAFFECTED = "UNAFFECTED"
    NULLIFIES_ACTION = "NULLIFIES_ACTION"
    RETARGETS_ACTION = "RETARGETS_ACTION"
    PREVENTS_DEATH = "PREVENTS_DEATH"
    RECORDS_VISIT = "RECORDS_VISIT"
    READS_RESOLVED_STATE = "READS_RESOLVED_STATE"
    SUPPRESSES_DEATH_REVEAL = "SUPPRESSES_DEATH_REVEAL"


class ResolutionMatrixRow(BaseModel):
    """One explicit action row in the Night Action Resolution matrix."""

    model_config = ConfigDict(frozen=True)

    action_kind: NightActionKind
    tier: NarTier
    blocked: MatrixEffect
    protected: MatrixEffect
    killed: MatrixEffect
    redirected: MatrixEffect
    watched_tracked: MatrixEffect
    cleaned: MatrixEffect
    records_visit: bool
    records_visit_when_blocked: bool


RESOLUTION_MATRIX: dict[NightActionKind, ResolutionMatrixRow] = {
    NightActionKind.ROLEBLOCK: ResolutionMatrixRow(
        action_kind=NightActionKind.ROLEBLOCK,
        tier=NarTier.ROLEBLOCK,
        blocked=MatrixEffect.UNAFFECTED,
        protected=MatrixEffect.UNAFFECTED,
        killed=MatrixEffect.UNAFFECTED,
        redirected=MatrixEffect.UNAFFECTED,
        watched_tracked=MatrixEffect.RECORDS_VISIT,
        cleaned=MatrixEffect.UNAFFECTED,
        records_visit=True,
        records_visit_when_blocked=True,
    ),
    NightActionKind.REDIRECT: ResolutionMatrixRow(
        action_kind=NightActionKind.REDIRECT,
        tier=NarTier.REDIRECT,
        blocked=MatrixEffect.NULLIFIES_ACTION,
        protected=MatrixEffect.UNAFFECTED,
        killed=MatrixEffect.UNAFFECTED,
        redirected=MatrixEffect.UNAFFECTED,
        watched_tracked=MatrixEffect.RECORDS_VISIT,
        cleaned=MatrixEffect.UNAFFECTED,
        records_visit=True,
        records_visit_when_blocked=False,
    ),
    NightActionKind.FACTIONAL_KILL: ResolutionMatrixRow(
        action_kind=NightActionKind.FACTIONAL_KILL,
        tier=NarTier.KILL_PROTECT_VISIT,
        blocked=MatrixEffect.NULLIFIES_ACTION,
        protected=MatrixEffect.PREVENTS_DEATH,
        killed=MatrixEffect.UNAFFECTED,
        redirected=MatrixEffect.RETARGETS_ACTION,
        watched_tracked=MatrixEffect.RECORDS_VISIT,
        cleaned=MatrixEffect.UNAFFECTED,
        records_visit=True,
        records_visit_when_blocked=False,
    ),
    NightActionKind.PROTECT: ResolutionMatrixRow(
        action_kind=NightActionKind.PROTECT,
        tier=NarTier.KILL_PROTECT_VISIT,
        blocked=MatrixEffect.NULLIFIES_ACTION,
        protected=MatrixEffect.PREVENTS_DEATH,
        killed=MatrixEffect.UNAFFECTED,
        redirected=MatrixEffect.RETARGETS_ACTION,
        watched_tracked=MatrixEffect.RECORDS_VISIT,
        cleaned=MatrixEffect.UNAFFECTED,
        records_visit=True,
        records_visit_when_blocked=False,
    ),
    NightActionKind.INVESTIGATE: ResolutionMatrixRow(
        action_kind=NightActionKind.INVESTIGATE,
        tier=NarTier.INVESTIGATION,
        blocked=MatrixEffect.NULLIFIES_ACTION,
        protected=MatrixEffect.UNAFFECTED,
        killed=MatrixEffect.READS_RESOLVED_STATE,
        redirected=MatrixEffect.RETARGETS_ACTION,
        watched_tracked=MatrixEffect.RECORDS_VISIT,
        cleaned=MatrixEffect.UNAFFECTED,
        records_visit=True,
        records_visit_when_blocked=False,
    ),
    NightActionKind.FRAME: ResolutionMatrixRow(
        action_kind=NightActionKind.FRAME,
        tier=NarTier.INVESTIGATION,
        blocked=MatrixEffect.NULLIFIES_ACTION,
        protected=MatrixEffect.UNAFFECTED,
        killed=MatrixEffect.READS_RESOLVED_STATE,
        redirected=MatrixEffect.RETARGETS_ACTION,
        watched_tracked=MatrixEffect.RECORDS_VISIT,
        cleaned=MatrixEffect.UNAFFECTED,
        records_visit=True,
        records_visit_when_blocked=False,
    ),
    NightActionKind.TRACK: ResolutionMatrixRow(
        action_kind=NightActionKind.TRACK,
        tier=NarTier.INVESTIGATION,
        blocked=MatrixEffect.NULLIFIES_ACTION,
        protected=MatrixEffect.UNAFFECTED,
        killed=MatrixEffect.READS_RESOLVED_STATE,
        redirected=MatrixEffect.RETARGETS_ACTION,
        watched_tracked=MatrixEffect.RECORDS_VISIT,
        cleaned=MatrixEffect.UNAFFECTED,
        records_visit=True,
        records_visit_when_blocked=False,
    ),
    NightActionKind.WATCH: ResolutionMatrixRow(
        action_kind=NightActionKind.WATCH,
        tier=NarTier.INVESTIGATION,
        blocked=MatrixEffect.NULLIFIES_ACTION,
        protected=MatrixEffect.UNAFFECTED,
        killed=MatrixEffect.READS_RESOLVED_STATE,
        redirected=MatrixEffect.RETARGETS_ACTION,
        watched_tracked=MatrixEffect.RECORDS_VISIT,
        cleaned=MatrixEffect.UNAFFECTED,
        records_visit=True,
        records_visit_when_blocked=False,
    ),
    NightActionKind.CLEAN: ResolutionMatrixRow(
        action_kind=NightActionKind.CLEAN,
        tier=NarTier.DEATH_REVEAL,
        blocked=MatrixEffect.NULLIFIES_ACTION,
        protected=MatrixEffect.UNAFFECTED,
        killed=MatrixEffect.UNAFFECTED,
        redirected=MatrixEffect.RETARGETS_ACTION,
        watched_tracked=MatrixEffect.RECORDS_VISIT,
        cleaned=MatrixEffect.SUPPRESSES_DEATH_REVEAL,
        records_visit=True,
        records_visit_when_blocked=False,
    ),
}


class NightActionIntent(BaseModel):
    """A normalized matrix-level night action."""

    model_config = ConfigDict(frozen=True)

    actor: str
    kind: NightActionKind
    target: str | None = None
    redirect_target: str | None = None


class VisitRecord(BaseModel):
    """One visit recorded at the visit tier."""

    model_config = ConfigDict(frozen=True)

    actor: str
    target: str
    action_kind: NightActionKind
    blocked: bool


class NightFeedback(BaseModel):
    """Structured deterministic feedback produced by NAR."""

    model_config = ConfigDict(frozen=True)

    recipient: str
    code: str
    message: str
    target: str | None = None
    finding: Literal["MAFIA", "TOWN"] | None = None
    visited_player_ids: tuple[str, ...] = ()
    visitor_player_ids: tuple[str, ...] = ()


class DeathReveal(BaseModel):
    """Death-reveal outcome for a killed seat."""

    model_config = ConfigDict(frozen=True)

    public_player_id: str
    role: Role | None
    faction: Faction | None
    cleaned: bool


class ProtectOutcome(BaseModel):
    """Per-actor protect resolution."""

    model_config = ConfigDict(frozen=True)

    protected: str | None
    reason: str


class InvestigationOutcome(BaseModel):
    """Per-actor investigation resolution."""

    model_config = ConfigDict(frozen=True)

    target: str | None
    finding: Literal["MAFIA", "TOWN"] | None
    reason: str


class CurrentMafiaKillOutcome(BaseModel):
    """Legacy current-role mafia kill outcome expressed by NAR."""

    model_config = ConfigDict(frozen=True)

    target: str | None
    vote_tally: dict[str, int]
    reason: str


class MatrixNightResolution(BaseModel):
    """Composed outcome of a single night phase."""

    model_config = ConfigDict(frozen=True)

    eliminated: str | None
    protected: str | None
    detective_finding: tuple[str, Literal["MAFIA", "TOWN"]] | None
    mafia_kill_target: str | None
    mafia_vote_tally: dict[str, int] = Field(default_factory=dict)
    mafia_kill_reason: str = REASON_ALL_INVALID
    blocked_actor_ids: tuple[str, ...] = ()
    visits: tuple[VisitRecord, ...] = ()
    feedback: tuple[NightFeedback, ...] = ()
    death_reveals: tuple[DeathReveal, ...] = ()
    cleaned_deaths: tuple[str, ...] = ()
    protect_outcomes: dict[str, ProtectOutcome] = Field(default_factory=dict)
    investigation_outcomes: dict[str, InvestigationOutcome] = Field(default_factory=dict)

    def feedback_by_code(self, code: str) -> tuple[NightFeedback, ...]:
        """Return feedback entries carrying ``code`` in deterministic order."""
        return tuple(entry for entry in self.feedback if entry.code == code)


def _seat_order(state: GameState) -> dict[str, int]:
    return {seat.public_player_id: seat.seat_index for seat in state.seats}


def _sort_ids(state: GameState, ids: set[str]) -> tuple[str, ...]:
    order = _seat_order(state)
    return tuple(sorted(ids, key=lambda pid: (order.get(pid, 10_000), pid)))


def _sort_intents(
    state: GameState, intents: Sequence[NightActionIntent]
) -> list[NightActionIntent]:
    order = _seat_order(state)
    return sorted(
        intents,
        key=lambda intent: (
            order.get(intent.actor, 10_000),
            intent.actor,
            intent.kind.value,
            intent.target or "",
            intent.redirect_target or "",
        ),
    )


def _alive_seat(state: GameState, public_player_id: str | None) -> Seat | None:
    if public_player_id is None:
        return None
    seat = state.seat_by_public_id(public_player_id)
    if seat is None or not seat.alive:
        return None
    return seat


def _finding_for(target: Seat) -> Literal["MAFIA", "TOWN"]:
    if target.role is Role.GODFATHER:
        return FINDING_TOWN
    return FINDING_MAFIA if target.faction is Faction.MAFIA else FINDING_TOWN


def _valid_target_for_kind(
    state: GameState,
    actor: Seat,
    kind: NightActionKind,
    target_id: str | None,
    redirect_target_id: str | None,
) -> bool:
    target = _alive_seat(state, target_id)
    if target is None:
        return False

    if kind is NightActionKind.FACTIONAL_KILL:
        return actor.faction is Faction.MAFIA and target.faction is not Faction.MAFIA
    if kind is NightActionKind.PROTECT:
        return actor.role is Role.DOCTOR and target_id != actor.last_protected_target
    if kind is NightActionKind.INVESTIGATE:
        return actor.role is Role.DETECTIVE and target_id != actor.public_player_id
    if kind is NightActionKind.REDIRECT:
        return _alive_seat(state, redirect_target_id) is not None
    return kind in {
        NightActionKind.ROLEBLOCK,
        NightActionKind.FRAME,
        NightActionKind.TRACK,
        NightActionKind.WATCH,
        NightActionKind.CLEAN,
    }


def _valid_intents(
    state: GameState,
    intents: Sequence[NightActionIntent],
) -> tuple[NightActionIntent, ...]:
    valid: list[NightActionIntent] = []
    for intent in _sort_intents(state, intents):
        actor = _alive_seat(state, intent.actor)
        if actor is None:
            continue
        if not _valid_target_for_kind(
            state,
            actor,
            intent.kind,
            intent.target,
            intent.redirect_target,
        ):
            continue
        valid.append(intent)
    return tuple(valid)


def _blocked_feedback(intent: NightActionIntent) -> NightFeedback:
    return NightFeedback(
        recipient=intent.actor,
        code="ACTION_BLOCKED",
        message="Your night action was blocked.",
        target=intent.target,
    )


def _record_visit(
    visits: list[VisitRecord],
    intent: NightActionIntent,
    *,
    target: str | None = None,
    blocked: bool = False,
) -> None:
    row = RESOLUTION_MATRIX[intent.kind]
    visit_target = target if target is not None else intent.target
    if visit_target is None:
        return
    if row.records_visit and (not blocked or row.records_visit_when_blocked):
        visits.append(
            VisitRecord(
                actor=intent.actor,
                target=visit_target,
                action_kind=intent.kind,
                blocked=blocked,
            )
        )


def _tier_intents(
    intents: Sequence[NightActionIntent],
    kind: NightActionKind,
) -> tuple[NightActionIntent, ...]:
    return tuple(intent for intent in intents if intent.kind is kind)


def _apply_roleblocks(
    state: GameState,
    intents: Sequence[NightActionIntent],
    visits: list[VisitRecord],
) -> tuple[str, ...]:
    blocked = {intent.target for intent in _tier_intents(intents, NightActionKind.ROLEBLOCK)}
    for intent in _tier_intents(intents, NightActionKind.ROLEBLOCK):
        _record_visit(visits, intent)
    return _sort_ids(state, {pid for pid in blocked if pid is not None})


def _apply_redirects(
    intents: Sequence[NightActionIntent],
    blocked_actor_ids: tuple[str, ...],
    visits: list[VisitRecord],
    feedback: list[NightFeedback],
) -> dict[str, str]:
    blocked = set(blocked_actor_ids)
    redirects: dict[str, str] = {}
    for intent in _tier_intents(intents, NightActionKind.REDIRECT):
        if intent.actor in blocked:
            feedback.append(_blocked_feedback(intent))
            _record_visit(visits, intent, blocked=True)
            continue
        assert intent.target is not None
        assert intent.redirect_target is not None
        redirects.setdefault(intent.target, intent.redirect_target)
        _record_visit(visits, intent)
    return redirects


def _retarget_if_redirected(
    intent: NightActionIntent,
    redirects: Mapping[str, str],
) -> NightActionIntent:
    row = RESOLUTION_MATRIX[intent.kind]
    if row.redirected is MatrixEffect.RETARGETS_ACTION and intent.actor in redirects:
        return intent.model_copy(update={"target": redirects[intent.actor]})
    return intent


def _active_intents_after_blocks_and_redirects(
    state: GameState,
    intents: Sequence[NightActionIntent],
    blocked_actor_ids: tuple[str, ...],
    redirects: Mapping[str, str],
    visits: list[VisitRecord],
    feedback: list[NightFeedback],
) -> tuple[NightActionIntent, ...]:
    blocked = set(blocked_actor_ids)
    active: list[NightActionIntent] = []
    for intent in intents:
        if intent.kind in (NightActionKind.ROLEBLOCK, NightActionKind.REDIRECT):
            continue
        row = RESOLUTION_MATRIX[intent.kind]
        if intent.actor in blocked and row.blocked is MatrixEffect.NULLIFIES_ACTION:
            feedback.append(_blocked_feedback(intent))
            _record_visit(visits, intent, blocked=True)
            continue

        retargeted = _retarget_if_redirected(intent, redirects)
        actor = _alive_seat(state, retargeted.actor)
        if actor is None or not _valid_target_for_kind(
            state,
            actor,
            retargeted.kind,
            retargeted.target,
            retargeted.redirect_target,
        ):
            continue
        _record_visit(visits, retargeted)
        active.append(retargeted)
    return tuple(active)


def _resolve_protects(
    intents: Sequence[NightActionIntent],
) -> tuple[str | None, set[str], dict[str, ProtectOutcome]]:
    protected_targets: set[str] = set()
    outcomes: dict[str, ProtectOutcome] = {}
    first_protected: str | None = None
    for intent in _tier_intents(intents, NightActionKind.PROTECT):
        assert intent.target is not None
        protected_targets.add(intent.target)
        first_protected = first_protected or intent.target
        outcomes[intent.actor] = ProtectOutcome(
            protected=intent.target,
            reason=REASON_PROTECTED,
        )
    return first_protected, protected_targets, outcomes


def _resolve_mafia_kill(
    intents: Sequence[NightActionIntent],
) -> tuple[str | None, dict[str, int], str]:
    tally = Counter(
        intent.target
        for intent in _tier_intents(intents, NightActionKind.FACTIONAL_KILL)
        if intent.target is not None
    )
    vote_tally = dict(tally)
    if not tally:
        return None, {}, REASON_ALL_INVALID

    top_count = max(tally.values())
    winners = [pid for pid, count in tally.items() if count == top_count]
    if len(winners) == 1:
        return winners[0], vote_tally, REASON_UNIQUE_PLURALITY
    return None, vote_tally, REASON_TIE


def _protection_feedback(
    protect_intents: Sequence[NightActionIntent],
    mafia_kill_target: str | None,
    eliminated: str | None,
) -> tuple[NightFeedback, ...]:
    if mafia_kill_target is None or eliminated is not None:
        return ()
    return tuple(
        NightFeedback(
            recipient=intent.actor,
            code="PROTECTION_SUCCESSFUL",
            message="Your protection prevented a kill.",
            target=mafia_kill_target,
        )
        for intent in protect_intents
        if intent.target == mafia_kill_target
    )


def _resolve_investigations(
    state: GameState,
    intents: Sequence[NightActionIntent],
    eliminated: str | None,
) -> tuple[
    tuple[str, Literal["MAFIA", "TOWN"]] | None,
    dict[str, InvestigationOutcome],
    tuple[NightFeedback, ...],
]:
    detective_finding: tuple[str, Literal["MAFIA", "TOWN"]] | None = None
    outcomes: dict[str, InvestigationOutcome] = {}
    feedback: list[NightFeedback] = []
    for intent in _tier_intents(intents, NightActionKind.INVESTIGATE):
        assert intent.target is not None
        target = state.seat_by_public_id(intent.target)
        if target is None:
            continue
        finding = _finding_for(target)
        outcomes[intent.actor] = InvestigationOutcome(
            target=intent.target,
            finding=finding,
            reason=REASON_RESOLVED,
        )
        if eliminated == intent.actor:
            continue
        feedback.append(
            NightFeedback(
                recipient=intent.actor,
                code="INVESTIGATION_RESULT",
                message=f"Investigation result: {intent.target} is {finding}.",
                target=intent.target,
                finding=finding,
            )
        )
        actor = state.seat_by_public_id(intent.actor)
        if actor is not None and actor.role is Role.DETECTIVE and detective_finding is None:
            detective_finding = (intent.target, finding)
    return detective_finding, outcomes, tuple(feedback)


def _resolve_visit_graph_feedback(
    state: GameState,
    intents: Sequence[NightActionIntent],
    visits: Sequence[VisitRecord],
) -> tuple[NightFeedback, ...]:
    feedback: list[NightFeedback] = []
    for intent in _tier_intents(intents, NightActionKind.TRACK):
        assert intent.target is not None
        visited = {
            visit.target
            for visit in visits
            if visit.actor == intent.target and visit.target != intent.actor
        }
        feedback.append(
            NightFeedback(
                recipient=intent.actor,
                code="TRACK_RESULT",
                message=f"Track result: {intent.target} visited {len(visited)} player(s).",
                target=intent.target,
                visited_player_ids=_sort_ids(state, visited),
            )
        )
    for intent in _tier_intents(intents, NightActionKind.WATCH):
        assert intent.target is not None
        visitors = {
            visit.actor
            for visit in visits
            if visit.target == intent.target and visit.actor != intent.actor
        }
        feedback.append(
            NightFeedback(
                recipient=intent.actor,
                code="WATCH_RESULT",
                message=f"Watch result: {intent.target} was visited by {len(visitors)} player(s).",
                target=intent.target,
                visitor_player_ids=_sort_ids(state, visitors),
            )
        )
    return tuple(feedback)


def _resolve_cleaned_deaths(
    intents: Sequence[NightActionIntent],
    eliminated: str | None,
) -> tuple[str, ...]:
    if eliminated is None:
        return ()
    cleaned = {
        intent.target
        for intent in _tier_intents(intents, NightActionKind.CLEAN)
        if intent.target == eliminated
    }
    return tuple(pid for pid in (eliminated,) if pid in cleaned)


def _death_reveals(
    state: GameState,
    eliminated: str | None,
    cleaned_deaths: tuple[str, ...],
) -> tuple[DeathReveal, ...]:
    if eliminated is None:
        return ()
    seat = state.seat_by_public_id(eliminated)
    if seat is None:
        return ()
    cleaned = eliminated in set(cleaned_deaths)
    return (
        DeathReveal(
            public_player_id=eliminated,
            role=None if cleaned else seat.role,
            faction=None if cleaned else seat.faction,
            cleaned=cleaned,
        ),
    )


def resolve_night_actions(
    state: GameState,
    intents: Sequence[NightActionIntent],
) -> MatrixNightResolution:
    """Resolve normalized night actions through the NAR matrix."""
    valid_intents = _valid_intents(state, intents)
    visits: list[VisitRecord] = []
    feedback: list[NightFeedback] = []

    blocked_actor_ids = _apply_roleblocks(state, valid_intents, visits)
    redirects = _apply_redirects(valid_intents, blocked_actor_ids, visits, feedback)
    active_intents = _active_intents_after_blocks_and_redirects(
        state,
        valid_intents,
        blocked_actor_ids,
        redirects,
        visits,
        feedback,
    )

    protected, protected_targets, protect_outcomes = _resolve_protects(active_intents)
    mafia_kill_target, mafia_vote_tally, mafia_kill_reason = _resolve_mafia_kill(active_intents)
    eliminated = (
        mafia_kill_target
        if mafia_kill_target is not None and mafia_kill_target not in protected_targets
        else None
    )
    feedback.extend(
        _protection_feedback(
            _tier_intents(active_intents, NightActionKind.PROTECT),
            mafia_kill_target,
            eliminated,
        )
    )

    detective_finding, investigation_outcomes, investigation_feedback = _resolve_investigations(
        state,
        active_intents,
        eliminated,
    )
    feedback.extend(investigation_feedback)
    feedback.extend(_resolve_visit_graph_feedback(state, active_intents, visits))

    cleaned_deaths = _resolve_cleaned_deaths(active_intents, eliminated)
    death_reveals = _death_reveals(state, eliminated, cleaned_deaths)

    return MatrixNightResolution(
        eliminated=eliminated,
        protected=protected,
        detective_finding=detective_finding,
        mafia_kill_target=mafia_kill_target,
        mafia_vote_tally=mafia_vote_tally,
        mafia_kill_reason=mafia_kill_reason,
        blocked_actor_ids=blocked_actor_ids,
        visits=tuple(visits),
        feedback=tuple(feedback),
        death_reveals=death_reveals,
        cleaned_deaths=cleaned_deaths,
        protect_outcomes=protect_outcomes,
        investigation_outcomes=investigation_outcomes,
    )


def _intents_from_current_submissions(
    submissions: Mapping[str, Action],
) -> tuple[NightActionIntent, ...]:
    intents: list[NightActionIntent] = []
    for actor, action in submissions.items():
        if action.type is ActionType.MAFIA_KILL:
            intents.append(
                NightActionIntent(
                    actor=actor,
                    kind=NightActionKind.FACTIONAL_KILL,
                    target=action.target,
                )
            )
        elif action.type is ActionType.PROTECT:
            intents.append(
                NightActionIntent(actor=actor, kind=NightActionKind.PROTECT, target=action.target)
            )
        elif action.type is ActionType.INVESTIGATE:
            intents.append(
                NightActionIntent(
                    actor=actor,
                    kind=NightActionKind.INVESTIGATE,
                    target=action.target,
                )
            )
        elif action.type is ActionType.ROLEBLOCK:
            intents.append(
                NightActionIntent(actor=actor, kind=NightActionKind.ROLEBLOCK, target=action.target)
            )
        elif action.type is ActionType.FRAME:
            intents.append(
                NightActionIntent(actor=actor, kind=NightActionKind.FRAME, target=action.target)
            )
        elif action.type is ActionType.TRACK:
            intents.append(
                NightActionIntent(actor=actor, kind=NightActionKind.TRACK, target=action.target)
            )
        elif action.type is ActionType.WATCH:
            intents.append(
                NightActionIntent(actor=actor, kind=NightActionKind.WATCH, target=action.target)
            )
        elif action.type is ActionType.CLEAN:
            intents.append(
                NightActionIntent(actor=actor, kind=NightActionKind.CLEAN, target=action.target)
            )
    return tuple(intents)


def resolve_current_night(
    state: GameState,
    all_submissions: Mapping[str, Action],
) -> MatrixNightResolution:
    """Resolve current ruleset submissions through the NAR matrix."""
    return resolve_night_actions(state, _intents_from_current_submissions(all_submissions))


def resolve_current_mafia_kill(
    state: GameState,
    mafia_submissions: Mapping[str, Action],
) -> CurrentMafiaKillOutcome:
    """Resolve current mafia-kill submissions through the NAR matrix."""
    result = resolve_night_actions(state, _intents_from_current_submissions(mafia_submissions))
    return CurrentMafiaKillOutcome(
        target=result.mafia_kill_target,
        vote_tally=dict(result.mafia_vote_tally),
        reason=result.mafia_kill_reason,
    )


def _find_role(state: GameState, role: Role) -> Seat | None:
    for seat in state.seats:
        if seat.role is role:
            return seat
    return None


def resolve_current_doctor_protect(
    state: GameState,
    doctor_submission: Action | None,
) -> ProtectOutcome:
    """Resolve the current Doctor protect action through the NAR matrix."""
    doctor = _find_role(state, Role.DOCTOR)
    if doctor is None:
        return ProtectOutcome(protected=None, reason=REASON_NO_DOCTOR)
    if not doctor.alive:
        return ProtectOutcome(protected=None, reason=REASON_DEAD_DOCTOR)
    if doctor_submission is None:
        return ProtectOutcome(protected=None, reason=REASON_NO_SUBMISSION)
    if doctor_submission.type is not ActionType.PROTECT or doctor_submission.target is None:
        return ProtectOutcome(protected=None, reason=REASON_INVALID_TARGET)
    target = _alive_seat(state, doctor_submission.target)
    if target is None:
        return ProtectOutcome(protected=None, reason=REASON_INVALID_TARGET)
    if doctor_submission.target == doctor.last_protected_target:
        return ProtectOutcome(protected=None, reason=REASON_REPEAT_VIOLATION)

    result = resolve_night_actions(
        state,
        (
            NightActionIntent(
                actor=doctor.public_player_id,
                kind=NightActionKind.PROTECT,
                target=doctor_submission.target,
            ),
        ),
    )
    return result.protect_outcomes.get(
        doctor.public_player_id,
        ProtectOutcome(protected=None, reason=REASON_INVALID_TARGET),
    )


def resolve_current_detective_investigation(
    state: GameState,
    detective_submission: Action | None,
) -> InvestigationOutcome:
    """Resolve the current Detective investigation through the NAR matrix."""
    detective = _find_role(state, Role.DETECTIVE)
    if detective is None:
        return InvestigationOutcome(target=None, finding=None, reason=REASON_NO_DETECTIVE)
    if not detective.alive:
        return InvestigationOutcome(target=None, finding=None, reason=REASON_DEAD_DETECTIVE)
    if detective_submission is None:
        return InvestigationOutcome(target=None, finding=None, reason=REASON_NO_SUBMISSION)
    if detective_submission.type is not ActionType.INVESTIGATE:
        return InvestigationOutcome(target=None, finding=None, reason=REASON_INVALID_TARGET)
    if detective_submission.target is None:
        return InvestigationOutcome(target=None, finding=None, reason=REASON_INVALID_TARGET)
    if detective_submission.target == detective.public_player_id:
        return InvestigationOutcome(target=None, finding=None, reason=REASON_SELF_TARGET)
    target = _alive_seat(state, detective_submission.target)
    if target is None:
        return InvestigationOutcome(target=None, finding=None, reason=REASON_INVALID_TARGET)

    result = resolve_night_actions(
        state,
        (
            NightActionIntent(
                actor=detective.public_player_id,
                kind=NightActionKind.INVESTIGATE,
                target=detective_submission.target,
            ),
        ),
    )
    return result.investigation_outcomes.get(
        detective.public_player_id,
        InvestigationOutcome(target=None, finding=None, reason=REASON_INVALID_TARGET),
    )


__all__ = [
    "FINDING_MAFIA",
    "FINDING_TOWN",
    "REASON_ALL_INVALID",
    "REASON_DEAD_DETECTIVE",
    "REASON_DEAD_DOCTOR",
    "REASON_INVALID_TARGET",
    "REASON_NO_DETECTIVE",
    "REASON_NO_DOCTOR",
    "REASON_NO_SUBMISSION",
    "REASON_PROTECTED",
    "REASON_REPEAT_VIOLATION",
    "REASON_RESOLVED",
    "REASON_SELF_TARGET",
    "REASON_TIE",
    "REASON_UNIQUE_PLURALITY",
    "RESOLUTION_MATRIX",
    "TIER_ORDER",
    "CurrentMafiaKillOutcome",
    "DeathReveal",
    "InvestigationOutcome",
    "MatrixEffect",
    "MatrixNightResolution",
    "NarTier",
    "NightActionIntent",
    "NightActionKind",
    "NightFeedback",
    "ProtectOutcome",
    "ResolutionMatrixRow",
    "VisitRecord",
    "resolve_current_detective_investigation",
    "resolve_current_doctor_protect",
    "resolve_current_mafia_kill",
    "resolve_current_night",
    "resolve_night_actions",
]
