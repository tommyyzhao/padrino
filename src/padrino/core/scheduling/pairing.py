"""Pure campaign pairing-matrix generation.

Campaign pairing produces a deterministic, coverage-guaranteed subset of the
field rather than an exhaustive round-robin. SWISS and rating-adaptive pairing
are intentionally out of scope for this pure core helper.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from math import gcd
from typing import Final, TypeAlias, TypeVar

from padrino.core.engine.rng import SeededRng
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.engine.state import Seat
from padrino.core.enums import Faction, RoleFamily
from padrino.core.rulesets import Ruleset, get_ruleset

PairingCell: TypeAlias = tuple[int, tuple[str, ...]]
BucketT = TypeVar("BucketT", Faction, RoleFamily)

_CELL_INDEX_BYTES: Final[int] = 8
_BALANCE_DENOMINATOR: Final[int] = 10_000
_BALANCE_TOLERANCE_BPS: Final[int] = 1_000
_MIN_BALANCE_TOLERANCE: Final[int] = 2
_MAX_EXTRA_BALANCING_ROUNDS: Final[int] = 12


class PairingFormat(StrEnum):
    """Supported campaign pairing formats."""

    MIRROR = "MIRROR"
    ROUND_ROBIN = "ROUND_ROBIN"


@dataclass(frozen=True, slots=True)
class ExposureBalanceViolation:
    """One model exposure bucket outside the configured tolerance."""

    model_id: str
    bucket: str
    actual: int
    expected_numerator: int
    expected_denominator: int
    tolerance: int


@dataclass(frozen=True, slots=True)
class PairingMatrixDiagnostics:
    """Coverage and exposure diagnostics for a pairing matrix."""

    required_cell_appearances: int
    minimum_distinct_opponents: int
    appearances_by_model: Mapping[str, int]
    distinct_opponents_by_model: Mapping[str, int]
    faction_counts_by_model: Mapping[str, Mapping[Faction, int]]
    role_family_counts_by_model: Mapping[str, Mapping[RoleFamily, int]]
    faction_balance_violations: tuple[ExposureBalanceViolation, ...]
    role_family_balance_violations: tuple[ExposureBalanceViolation, ...]


@dataclass(frozen=True, slots=True)
class _SeatContribution:
    factions: Mapping[Faction, int]
    role_families: Mapping[RoleFamily, int]
    exposure_count: int


def generate_pairing_matrix(
    campaign_seed: str,
    ruleset_id: str,
    model_field: Sequence[str],
    *,
    format: PairingFormat | str,
    per_model_game_target: int,
) -> list[PairingCell]:
    """Return an ordered deterministic subset pairing matrix.

    Each returned cell is ``(cell_index, roster)``. ``roster`` contains one
    model id per ruleset seat. In ``MIRROR`` format, a cell is intended to be
    materialized as a two-leg mirror pair: leg 0 uses ``roster`` and leg 1 uses
    ``tuple(reversed(roster))`` while both legs share the cell seed from
    :func:`derive_campaign_cell_seed`.
    """
    pairing_format = _normalize_format(format)
    ruleset = get_ruleset(ruleset_id)
    models = _validate_model_field(model_field, ruleset.PLAYER_COUNT)
    required_appearances = required_cell_appearances(pairing_format, per_model_game_target)
    base_rounds = _ceil_div(required_appearances, ruleset.PLAYER_COUNT)

    last_diagnostics: PairingMatrixDiagnostics | None = None
    for extra_rounds in range(_MAX_EXTRA_BALANCING_ROUNDS + 1):
        matrix = _build_candidate_matrix(
            campaign_seed,
            ruleset,
            models,
            pairing_format,
            rounds=base_rounds + extra_rounds,
        )
        diagnostics = analyze_pairing_matrix(
            campaign_seed,
            ruleset_id,
            matrix,
            format=pairing_format,
            per_model_game_target=per_model_game_target,
            model_field=models,
        )
        if _diagnostics_satisfy_guarantees(diagnostics):
            return matrix
        last_diagnostics = diagnostics

    raise RuntimeError(
        f"unable to generate a pairing matrix within exposure tolerances: {last_diagnostics!r}"
    )


def derive_campaign_cell_seed(campaign_seed: str, cell_index: int) -> str:
    """Return ``sha256_hex(b'campaign:' + campaign_seed + cell_index_be8)``."""
    if cell_index < 0:
        raise ValueError("cell_index must be >= 0")
    if cell_index >= 1 << (_CELL_INDEX_BYTES * 8):
        raise ValueError("cell_index is too large for the campaign seed derivation")
    return hashlib.sha256(
        b"campaign:" + campaign_seed.encode("utf-8") + cell_index.to_bytes(_CELL_INDEX_BYTES, "big")
    ).hexdigest()


def mirror_leg_roster(roster: Sequence[str], *, pair_leg: int) -> tuple[str, ...]:
    """Return the seat-order roster for one mirror-pair leg."""
    if pair_leg == 0:
        return tuple(roster)
    if pair_leg == 1:
        return tuple(reversed(roster))
    raise ValueError("pair_leg must be 0 or 1")


def required_cell_appearances(
    format: PairingFormat | str,
    per_model_game_target: int,
) -> int:
    """Return the per-model cell appearances needed to hit the game target."""
    if per_model_game_target <= 0:
        raise ValueError("per_model_game_target must be > 0")
    pairing_format = _normalize_format(format)
    return _ceil_div(per_model_game_target, _legs_per_cell(pairing_format))


def minimum_distinct_opponents(
    field_size: int,
    player_count: int,
    required_cell_appearances: int,
) -> int:
    """Return the configured distinct-opponent floor for a generated matrix."""
    if player_count <= 1:
        raise ValueError("player_count must be > 1")
    if field_size < player_count:
        raise ValueError("field_size must be >= player_count")
    if required_cell_appearances <= 0:
        raise ValueError("required_cell_appearances must be > 0")
    return min(
        field_size - 1,
        max(player_count - 1, player_count * 2),
    )


def analyze_pairing_matrix(
    campaign_seed: str,
    ruleset_id: str,
    matrix: Sequence[PairingCell],
    *,
    format: PairingFormat | str,
    per_model_game_target: int,
    model_field: Sequence[str] | None = None,
) -> PairingMatrixDiagnostics:
    """Compute pure coverage and exposure diagnostics for ``matrix``."""
    pairing_format = _normalize_format(format)
    ruleset = get_ruleset(ruleset_id)
    models = _model_universe(matrix, model_field)
    appearances: dict[str, int] = dict.fromkeys(models, 0)
    opponents: dict[str, set[str]] = {model: set() for model in models}
    faction_counts: dict[str, Counter[Faction]] = {model: Counter() for model in models}
    role_family_counts: dict[str, Counter[RoleFamily]] = {model: Counter() for model in models}

    for expected_cell_index, (cell_index, roster) in enumerate(matrix):
        if cell_index != expected_cell_index:
            raise ValueError(
                f"matrix cell indices must be contiguous from 0; got {cell_index} "
                f"at position {expected_cell_index}"
            )
        if len(roster) != ruleset.PLAYER_COUNT:
            raise ValueError(
                f"roster for cell {cell_index} must have {ruleset.PLAYER_COUNT} models"
            )
        if len(set(roster)) != len(roster):
            raise ValueError(f"roster for cell {cell_index} contains duplicate models")

        seats = assign_roles(derive_campaign_cell_seed(campaign_seed, cell_index), ruleset)
        contributions = _seat_contributions(seats, ruleset, pairing_format)
        roster_set = set(roster)
        for position, model_id in enumerate(roster):
            if model_id not in appearances:
                raise ValueError(f"model {model_id!r} is not part of the matrix model field")
            appearances[model_id] += 1
            opponents[model_id].update(opponent for opponent in roster_set if opponent != model_id)
            faction_counts[model_id].update(contributions[position].factions)
            role_family_counts[model_id].update(contributions[position].role_families)

    required_appearances = required_cell_appearances(pairing_format, per_model_game_target)
    min_opponents = minimum_distinct_opponents(
        len(models),
        ruleset.PLAYER_COUNT,
        required_appearances,
    )
    faction_totals, role_family_totals = _ruleset_bucket_totals(ruleset)
    faction_maps = {model: dict(counts) for model, counts in faction_counts.items()}
    role_family_maps = {model: dict(counts) for model, counts in role_family_counts.items()}
    return PairingMatrixDiagnostics(
        required_cell_appearances=required_appearances,
        minimum_distinct_opponents=min_opponents,
        appearances_by_model=dict(appearances),
        distinct_opponents_by_model={
            model: len(model_opponents) for model, model_opponents in opponents.items()
        },
        faction_counts_by_model=faction_maps,
        role_family_counts_by_model=role_family_maps,
        faction_balance_violations=_balance_violations(
            models,
            faction_maps,
            faction_totals,
            ruleset.PLAYER_COUNT,
        ),
        role_family_balance_violations=_balance_violations(
            models,
            role_family_maps,
            role_family_totals,
            ruleset.PLAYER_COUNT,
        ),
    )


def _build_candidate_matrix(
    campaign_seed: str,
    ruleset: Ruleset,
    models: tuple[str, ...],
    pairing_format: PairingFormat,
    *,
    rounds: int,
) -> list[PairingCell]:
    player_count = ruleset.PLAYER_COUNT
    field_size = len(models)
    rng = SeededRng(_matrix_rng_seed(campaign_seed, ruleset.RULESET_ID, pairing_format, models))
    base_models = tuple(rng.shuffle(list(models)))
    strides = tuple(rng.shuffle(_coprime_strides(field_size)))
    faction_counts: dict[str, Counter[Faction]] = {model: Counter() for model in models}
    role_family_counts: dict[str, Counter[RoleFamily]] = {model: Counter() for model in models}
    exposure_counts: dict[str, int] = dict.fromkeys(models, 0)
    faction_totals, role_family_totals = _ruleset_bucket_totals(ruleset)

    matrix: list[PairingCell] = []
    for round_index in range(rounds):
        stride = strides[round_index % len(strides)]
        offset = rng.randbelow(field_size)
        round_models = tuple(rng.shuffle(list(base_models)))
        for start in range(field_size):
            cell_index = len(matrix)
            seats = assign_roles(derive_campaign_cell_seed(campaign_seed, cell_index), ruleset)
            contributions = _seat_contributions(seats, ruleset, pairing_format)
            group = [
                round_models[(offset + start + position * stride) % field_size]
                for position in range(player_count)
            ]
            roster = _assign_group_to_seats(
                group,
                contributions,
                faction_counts,
                role_family_counts,
                exposure_counts,
                faction_totals,
                role_family_totals,
                player_count,
            )
            matrix.append((cell_index, roster))

    return matrix


def _assign_group_to_seats(
    group: Sequence[str],
    contributions: Sequence[_SeatContribution],
    faction_counts: dict[str, Counter[Faction]],
    role_family_counts: dict[str, Counter[RoleFamily]],
    exposure_counts: dict[str, int],
    faction_totals: Mapping[Faction, int],
    role_family_totals: Mapping[RoleFamily, int],
    player_count: int,
) -> tuple[str, ...]:
    remaining = list(group)
    local_order = {model: order for order, model in enumerate(group)}
    assigned: dict[int, str] = {}
    positions = sorted(
        range(player_count),
        key=lambda position: _contribution_priority(contributions[position], position),
    )

    for position in positions:
        contribution = contributions[position]
        best_model = remaining[0]
        best_score = _assignment_score(
            best_model,
            contribution,
            faction_counts,
            role_family_counts,
            exposure_counts,
            faction_totals,
            role_family_totals,
            player_count,
            local_order,
        )
        for model in remaining[1:]:
            score = _assignment_score(
                model,
                contribution,
                faction_counts,
                role_family_counts,
                exposure_counts,
                faction_totals,
                role_family_totals,
                player_count,
                local_order,
            )
            if score < best_score:
                best_model = model
                best_score = score
        assigned[position] = best_model
        remaining.remove(best_model)
        faction_counts[best_model].update(contribution.factions)
        role_family_counts[best_model].update(contribution.role_families)
        exposure_counts[best_model] += contribution.exposure_count

    return tuple(assigned[position] for position in range(player_count))


def _assignment_score(
    model: str,
    contribution: _SeatContribution,
    faction_counts: Mapping[str, Counter[Faction]],
    role_family_counts: Mapping[str, Counter[RoleFamily]],
    exposure_counts: Mapping[str, int],
    faction_totals: Mapping[Faction, int],
    role_family_totals: Mapping[RoleFamily, int],
    player_count: int,
    local_order: Mapping[str, int],
) -> tuple[int, int, int]:
    total_after = exposure_counts[model] + contribution.exposure_count
    benefit = 0
    for faction, count in contribution.factions.items():
        desired = total_after * faction_totals[faction]
        current = faction_counts[model][faction] * player_count
        benefit += count * (desired - current)
    for role_family, count in contribution.role_families.items():
        desired = total_after * role_family_totals[role_family]
        current = role_family_counts[model][role_family] * player_count
        benefit += count * (desired - current)
    return (-benefit, exposure_counts[model], local_order[model])


def _contribution_priority(contribution: _SeatContribution, position: int) -> tuple[int, int, int]:
    mafia_exposures = contribution.factions.get(Faction.MAFIA, 0)
    non_vanilla_exposures = sum(
        count
        for role_family, count in contribution.role_families.items()
        if role_family is not RoleFamily.VANILLA_TOWN
    )
    return (-mafia_exposures, -non_vanilla_exposures, position)


def _seat_contributions(
    seats: Sequence[Seat],
    ruleset: Ruleset,
    pairing_format: PairingFormat,
) -> tuple[_SeatContribution, ...]:
    player_count = ruleset.PLAYER_COUNT
    contributions: list[_SeatContribution] = []
    for position in range(player_count):
        seat_indices = [position]
        if pairing_format is PairingFormat.MIRROR:
            seat_indices.append(player_count - position - 1)
        factions: Counter[Faction] = Counter()
        role_families: Counter[RoleFamily] = Counter()
        for seat_index in seat_indices:
            seat = seats[seat_index]
            factions[seat.faction] += 1
            role_families[ruleset.role_family_for(seat.role)] += 1
        contributions.append(
            _SeatContribution(
                factions=dict(factions),
                role_families=dict(role_families),
                exposure_count=len(seat_indices),
            )
        )
    return tuple(contributions)


def _balance_violations(
    models: Sequence[str],
    counts_by_model: Mapping[str, Mapping[BucketT, int]],
    bucket_totals: Mapping[BucketT, int],
    player_count: int,
) -> tuple[ExposureBalanceViolation, ...]:
    violations: list[ExposureBalanceViolation] = []
    sorted_buckets = sorted(bucket_totals, key=lambda bucket: str(bucket))
    for model in models:
        counts = counts_by_model[model]
        total_exposures = sum(counts.values())
        tolerance = _balance_tolerance(total_exposures)
        for bucket in sorted_buckets:
            actual = counts.get(bucket, 0)
            expected_numerator = total_exposures * bucket_totals[bucket]
            diff_numerator = abs(actual * player_count - expected_numerator)
            if diff_numerator > tolerance * player_count:
                violations.append(
                    ExposureBalanceViolation(
                        model_id=model,
                        bucket=str(bucket),
                        actual=actual,
                        expected_numerator=expected_numerator,
                        expected_denominator=player_count,
                        tolerance=tolerance,
                    )
                )
    return tuple(violations)


def _diagnostics_satisfy_guarantees(diagnostics: PairingMatrixDiagnostics) -> bool:
    return (
        all(
            appearances >= diagnostics.required_cell_appearances
            for appearances in diagnostics.appearances_by_model.values()
        )
        and all(
            opponents >= diagnostics.minimum_distinct_opponents
            for opponents in diagnostics.distinct_opponents_by_model.values()
        )
        and diagnostics.faction_balance_violations == ()
        and diagnostics.role_family_balance_violations == ()
    )


def _ruleset_bucket_totals(
    ruleset: Ruleset,
) -> tuple[dict[Faction, int], dict[RoleFamily, int]]:
    faction_totals: dict[Faction, int] = {}
    role_family_totals: dict[RoleFamily, int] = {}
    for role, count in ruleset.ROLE_COUNTS.items():
        faction = ruleset.faction_for(role)
        role_family = ruleset.role_family_for(role)
        faction_totals[faction] = faction_totals.get(faction, 0) + count
        role_family_totals[role_family] = role_family_totals.get(role_family, 0) + count
    return faction_totals, role_family_totals


def _model_universe(
    matrix: Sequence[PairingCell],
    model_field: Sequence[str] | None,
) -> tuple[str, ...]:
    if model_field is not None:
        return _validate_model_field(model_field, 1)
    ordered: list[str] = []
    seen: set[str] = set()
    for _cell_index, roster in matrix:
        for model in roster:
            if model not in seen:
                ordered.append(model)
                seen.add(model)
    if not ordered:
        raise ValueError("matrix must contain at least one roster")
    return tuple(ordered)


def _validate_model_field(model_field: Sequence[str], player_count: int) -> tuple[str, ...]:
    models = tuple(model_field)
    if len(models) < player_count:
        raise ValueError(
            f"model_field must contain at least {player_count} models, got {len(models)}"
        )
    if len(set(models)) != len(models):
        raise ValueError("model_field must not contain duplicate model ids")
    if any(model == "" for model in models):
        raise ValueError("model_field must not contain empty model ids")
    return models


def _matrix_rng_seed(
    campaign_seed: str,
    ruleset_id: str,
    pairing_format: PairingFormat,
    models: Sequence[str],
) -> bytes:
    payload = bytearray()
    for part in (campaign_seed, ruleset_id, pairing_format.value):
        encoded = part.encode("utf-8")
        payload.extend(len(encoded).to_bytes(4, "big"))
        payload.extend(encoded)
    for model in models:
        encoded = model.encode("utf-8")
        payload.extend(len(encoded).to_bytes(4, "big"))
        payload.extend(encoded)
    return hashlib.sha256(bytes(payload)).digest()


def _coprime_strides(field_size: int) -> list[int]:
    return [stride for stride in range(1, field_size) if gcd(stride, field_size) == 1]


def _legs_per_cell(pairing_format: PairingFormat) -> int:
    if pairing_format is PairingFormat.MIRROR:
        return 2
    return 1


def _balance_tolerance(total_exposures: int) -> int:
    return max(
        _MIN_BALANCE_TOLERANCE,
        _ceil_div(total_exposures * _BALANCE_TOLERANCE_BPS, _BALANCE_DENOMINATOR),
    )


def _normalize_format(format: PairingFormat | str) -> PairingFormat:
    try:
        return PairingFormat(format)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in PairingFormat)
        raise ValueError(f"format must be one of {allowed}") from exc


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


__all__ = [
    "ExposureBalanceViolation",
    "PairingCell",
    "PairingFormat",
    "PairingMatrixDiagnostics",
    "analyze_pairing_matrix",
    "derive_campaign_cell_seed",
    "generate_pairing_matrix",
    "minimum_distinct_opponents",
    "mirror_leg_roster",
    "required_cell_appearances",
]
