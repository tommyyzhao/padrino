"""Canonical-team ruleset purity checks.

The scientific ladder is intentionally narrow: canonical ELO is only valid for
two-team Town-versus-Mafia games. This module gives tests and callers one pure
assertion that a ruleset marked canonical has only those ranked outcomes, maps a
draw to an equal-rank tie, and declares no solo, conversion, alt-win, or
kingmaking mechanics.
"""

from __future__ import annotations

from typing import Literal, Protocol

from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.engine.win_conditions import check_win
from padrino.core.enums import Faction, PhaseKind, RatingContextKind, Role

CanonicalOutcome = Literal["TOWN", "MAFIA", "DRAW"]
CanonicalTeam = Literal["TOWN", "MAFIA"]
CanonicalRanks = dict[CanonicalTeam, int]

_CANONICAL_TEAMS: frozenset[Faction] = frozenset({Faction.TOWN, Faction.MAFIA})


class CanonicalRulesetError(AssertionError):
    """Raised when a canonical ruleset declaration is not canonical-pure."""


class CanonicalRuleset(Protocol):
    """Ruleset surface needed to audit canonical-team purity."""

    RULESET_ID: str
    RATING_CONTEXT_KIND: RatingContextKind
    IS_CANONICAL: bool
    PLAYER_COUNT: int
    ROLE_COUNTS: dict[Role, int]
    ROLE_FACTIONS: dict[Role, Faction]
    MAX_DAYS: int
    ALT_WIN_CONDITIONS: tuple[str, ...]
    SOLO_FACTIONS: tuple[str, ...]
    FACTION_MUTATION_ALLOWED: bool
    KINGMAKING_OBJECTIVE: bool


def canonical_team_ranks_for_outcome(outcome: CanonicalOutcome) -> CanonicalRanks:
    """Return OpenSkill ranks for a canonical outcome; lower rank is better."""
    if outcome == "TOWN":
        return {"TOWN": 1, "MAFIA": 2}
    if outcome == "MAFIA":
        return {"TOWN": 2, "MAFIA": 1}
    return {"TOWN": 1, "MAFIA": 1}


def assert_ruleset_canonical_pure(ruleset: CanonicalRuleset) -> None:
    """Assert that a canonical ruleset is safe for the canonical ELO ladder."""
    if ruleset.RATING_CONTEXT_KIND is not RatingContextKind.CANONICAL_TEAM:
        raise CanonicalRulesetError(
            f"{ruleset.RULESET_ID} is canonical but does not declare CANONICAL_TEAM"
        )
    if not ruleset.IS_CANONICAL:
        raise CanonicalRulesetError(f"{ruleset.RULESET_ID} is not marked canonical")
    if ruleset.ALT_WIN_CONDITIONS:
        raise CanonicalRulesetError(
            f"{ruleset.RULESET_ID} has alt-win conditions: {ruleset.ALT_WIN_CONDITIONS!r}"
        )
    if ruleset.SOLO_FACTIONS:
        raise CanonicalRulesetError(
            f"{ruleset.RULESET_ID} has solo factions: {ruleset.SOLO_FACTIONS!r}"
        )
    if ruleset.FACTION_MUTATION_ALLOWED:
        raise CanonicalRulesetError(f"{ruleset.RULESET_ID} allows faction mutation")
    if ruleset.KINGMAKING_OBJECTIVE:
        raise CanonicalRulesetError(f"{ruleset.RULESET_ID} has a kingmaking objective")

    faction_totals = _assert_two_team_role_space(ruleset)
    observed = _observed_terminal_outcomes(ruleset, faction_totals)
    invalid = observed - {"TOWN", "MAFIA", "DRAW"}
    if invalid:
        raise CanonicalRulesetError(
            f"{ruleset.RULESET_ID} has non-canonical terminal winners: {sorted(invalid)}"
        )
    ranked_outcomes = observed - {"DRAW"}
    if ranked_outcomes != {"TOWN", "MAFIA"}:
        raise CanonicalRulesetError(
            f"{ruleset.RULESET_ID} ranked outcomes must be exactly Town and Mafia; "
            f"observed {sorted(ranked_outcomes)}"
        )
    if "DRAW" in observed and len(set(canonical_team_ranks_for_outcome("DRAW").values())) != 1:
        raise CanonicalRulesetError(f"{ruleset.RULESET_ID} draw is not a team tie")


def _assert_two_team_role_space(ruleset: CanonicalRuleset) -> dict[Faction, int]:
    role_total = sum(ruleset.ROLE_COUNTS.values())
    if role_total != ruleset.PLAYER_COUNT:
        raise CanonicalRulesetError(
            f"{ruleset.RULESET_ID} role count {role_total} != player count {ruleset.PLAYER_COUNT}"
        )

    totals: dict[Faction, int] = {Faction.TOWN: 0, Faction.MAFIA: 0}
    for role, count in ruleset.ROLE_COUNTS.items():
        faction = ruleset.ROLE_FACTIONS.get(role)
        if faction not in _CANONICAL_TEAMS:
            raise CanonicalRulesetError(
                f"{ruleset.RULESET_ID} role {role.value} has non-canonical faction {faction!r}"
            )
        if count <= 0:
            raise CanonicalRulesetError(
                f"{ruleset.RULESET_ID} role {role.value} has non-positive count {count}"
            )
        totals[faction] += count

    if set(totals) != _CANONICAL_TEAMS or any(total <= 0 for total in totals.values()):
        raise CanonicalRulesetError(f"{ruleset.RULESET_ID} must contain both Town and Mafia teams")
    return totals


def _observed_terminal_outcomes(
    ruleset: CanonicalRuleset,
    faction_totals: dict[Faction, int],
) -> set[str]:
    observed: set[str] = set()
    for alive_town in range(faction_totals[Faction.TOWN] + 1):
        for alive_mafia in range(faction_totals[Faction.MAFIA] + 1):
            if alive_town + alive_mafia == 0:
                continue
            for day in range(1, ruleset.MAX_DAYS + 2):
                state = _state_with_alive_counts(
                    ruleset,
                    alive_by_faction={
                        Faction.TOWN: alive_town,
                        Faction.MAFIA: alive_mafia,
                    },
                    day=day,
                )
                win = check_win(state, ruleset)
                if win is not None:
                    observed.add(win.winner)
    return observed


def _state_with_alive_counts(
    ruleset: CanonicalRuleset,
    *,
    alive_by_faction: dict[Faction, int],
    day: int,
) -> GameState:
    remaining_alive = dict(alive_by_faction)
    seats: list[Seat] = []
    seat_index = 0
    for role, count in ruleset.ROLE_COUNTS.items():
        faction = ruleset.ROLE_FACTIONS[role]
        for _ in range(count):
            alive = remaining_alive[faction] > 0
            if alive:
                remaining_alive[faction] -= 1
            seats.append(
                Seat(
                    public_player_id=f"P{seat_index + 1:02d}",
                    seat_index=seat_index,
                    role=role,
                    faction=faction,
                    alive=alive,
                )
            )
            seat_index += 1

    return GameState(
        ruleset_id=ruleset.RULESET_ID,
        game_id="canonicality-audit",
        game_seed="canonicality-audit",
        current_phase=Phase(kind=PhaseKind.DAY_VOTE, day=day, round=0),
        seats=tuple(seats),
        day=day,
    )


__all__ = [
    "CanonicalOutcome",
    "CanonicalRanks",
    "CanonicalRuleset",
    "CanonicalRulesetError",
    "assert_ruleset_canonical_pure",
    "canonical_team_ranks_for_outcome",
]
