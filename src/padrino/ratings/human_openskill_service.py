"""OpenSkill updates for ranked Humans-Included human ELO.

This service is intentionally separate from the scientific rating writer. It
resolves only ``League.kind == HUMANS_INCLUDED`` rows that are also ranked, and
persists only the sibling ``human_rating`` / ``human_rating_event`` tables.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

import structlog
from openskill.models import PlackettLuce
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import Faction, LeagueKind
from padrino.core.ranked_eligibility import RankedEligibilityReason
from padrino.core.rulesets.canonicality import canonical_team_ranks_for_outcome
from padrino.db.models import Game, GameSeat, HumanRating, HumanRatingEvent, League
from padrino.db.repositories import human_ratings as human_ratings_repo
from padrino.ratings.openskill_service import INITIAL_MU, INITIAL_SIGMA, SCOPE_GLOBAL
from padrino.ratings.openskill_service import SCOPE_VALUE_GLOBAL as _SCOPE_VALUE_GLOBAL

SCOPE_VALUE_GLOBAL: Final[str] = _SCOPE_VALUE_GLOBAL
_logger = structlog.get_logger("padrino.ratings.human_openskill_service")


class FirewallViolation(RuntimeError):
    """Raised when ranked human ELO is asked to write outside HUMANS_INCLUDED."""

    def __init__(self, *, league_id: uuid.UUID, league_kind: str | None) -> None:
        self.league_id = league_id
        self.league_kind = league_kind
        super().__init__(
            "human rating firewall violation: "
            f"league_id={league_id} kind={league_kind!r} is not HUMANS_INCLUDED"
        )


@dataclass(frozen=True, slots=True)
class HumanGameResult:
    """Per-game outcome consumed by the ranked human rating service."""

    game_id: uuid.UUID
    winner: Literal["TOWN", "MAFIA", "DRAW"]


@dataclass(frozen=True, slots=True)
class _TeamEntry:
    public_player_id: str
    row: HumanRating | None
    existing_event: HumanRatingEvent | None
    before_mu: float
    before_sigma: float
    rating: Any


def _conservative(mu: float, sigma: float) -> float:
    return mu - 3.0 * sigma


def ranked_human_rating_enabled(settings: Any | None = None) -> bool:
    """Read the ranked human-rating kill-switch, failing closed if absent."""
    if settings is None:
        from padrino.settings import get_settings

        settings = get_settings()
    return getattr(settings, "padrino_ranked_human_rating_enabled", False) is True


async def _resolve_ranked_humans_included_metadata(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    ranked: bool,
    game_id: uuid.UUID,
) -> tuple[League, Game] | None:
    if not ranked:
        return None
    league = await session.get(League, league_id)
    if league is None:
        return None
    if league.kind != LeagueKind.HUMANS_INCLUDED.value:
        _logger.error(
            "human.rating.firewall_violation",
            league_id=str(league_id),
            kind=league.kind,
        )
        raise FirewallViolation(league_id=league_id, league_kind=league.kind)
    if league.ranked is not True:
        return None
    game = await session.get(Game, game_id)
    if game is None or game.ruleset_id != league.ruleset_id:
        return None
    return league, game


async def _seat_rows(session: AsyncSession, game_id: uuid.UUID) -> list[GameSeat]:
    stmt = select(GameSeat).where(GameSeat.game_id == game_id).order_by(GameSeat.seat_index)
    return list((await session.execute(stmt)).scalars())


async def _team_entries(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    game_id: uuid.UUID,
    seats: Sequence[GameSeat],
    model: PlackettLuce,
) -> tuple[list[_TeamEntry], list[_TeamEntry]] | None:
    town: list[_TeamEntry] = []
    mafia: list[_TeamEntry] = []
    human_count = 0
    for seat in seats:
        try:
            faction = Faction(seat.faction)
        except ValueError:
            return None
        if faction not in {Faction.TOWN, Faction.MAFIA}:
            return None

        row: HumanRating | None = None
        existing_event: HumanRatingEvent | None = None
        before_mu = INITIAL_MU
        before_sigma = INITIAL_SIGMA
        if seat.occupant_principal_id is not None:
            human_count += 1
            row = await human_ratings_repo.get_or_create_human_rating(
                session,
                league_id=league_id,
                human_player_id=str(seat.occupant_principal_id),
                scope_type=SCOPE_GLOBAL,
                scope_value=SCOPE_VALUE_GLOBAL,
                initial_mu=INITIAL_MU,
                initial_sigma=INITIAL_SIGMA,
                initial_conservative_score=_conservative(INITIAL_MU, INITIAL_SIGMA),
            )
            existing_event = await human_ratings_repo.get_human_rating_event(
                session,
                game_id=game_id,
                human_player_id=row.human_player_id,
                scope_type=SCOPE_GLOBAL,
                scope_value=SCOPE_VALUE_GLOBAL,
            )
            if existing_event is None:
                before_mu = float(row.mu)
                before_sigma = float(row.sigma)
            else:
                before_mu = float(existing_event.before_mu)
                before_sigma = float(existing_event.before_sigma)
            rating = model.create_rating([before_mu, before_sigma], name=str(row.id))
        else:
            rating = model.create_rating([INITIAL_MU, INITIAL_SIGMA])

        entry = _TeamEntry(
            public_player_id=seat.public_player_id,
            row=row,
            existing_event=existing_event,
            before_mu=before_mu,
            before_sigma=before_sigma,
            rating=rating,
        )
        if faction is Faction.TOWN:
            town.append(entry)
        else:
            mafia.append(entry)

    if human_count == 0 or not town or not mafia:
        return None
    return town, mafia


async def _persist_human_entry(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    game_id: uuid.UUID,
    entry: _TeamEntry,
    new_mu: float,
    new_sigma: float,
    now: datetime,
) -> HumanRatingEvent | None:
    row = entry.row
    if row is None:
        return None
    if entry.existing_event is not None:
        return None
    event = await human_ratings_repo.record_human_rating_event(
        session,
        league_id=league_id,
        game_id=game_id,
        human_player_id=row.human_player_id,
        public_player_id=entry.public_player_id,
        scope_type=SCOPE_GLOBAL,
        scope_value=SCOPE_VALUE_GLOBAL,
        before_mu=entry.before_mu,
        before_sigma=entry.before_sigma,
        after_mu=new_mu,
        after_sigma=new_sigma,
    )
    if event is None:
        return None
    await human_ratings_repo.record_ranked_write_audit(
        session,
        league_id=league_id,
        game_id=game_id,
        human_player_id=row.human_player_id,
        decision="APPLIED",
        reason=RankedEligibilityReason.ELIGIBLE.value,
        flag_enabled=True,
        created_at=now,
    )
    updated = await human_ratings_repo.update_human_rating(
        session,
        row.id,
        mu=new_mu,
        sigma=new_sigma,
        conservative_score=_conservative(new_mu, new_sigma),
        games=row.games + 1,
        last_game_at=now,
    )
    if updated is None:  # pragma: no cover - row was just inserted in this txn.
        msg = f"Human rating row {row.id} disappeared between insert and update"
        raise RuntimeError(msg)
    return event


async def _record_ranked_disabled_refusal(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    ranked: bool,
    game_id: uuid.UUID,
    now: datetime,
) -> None:
    if not ranked:
        return
    league = await session.get(League, league_id)
    if (
        league is None
        or league.kind != LeagueKind.HUMANS_INCLUDED.value
        or league.ranked is not True
    ):
        return
    game = await session.get(Game, game_id)
    if game is None or game.ruleset_id != league.ruleset_id:
        return
    await human_ratings_repo.record_ranked_write_audit(
        session,
        league_id=league_id,
        game_id=game_id,
        human_player_id=None,
        decision="REFUSED",
        reason=RankedEligibilityReason.RANKED_DISABLED.value,
        flag_enabled=False,
        created_at=now,
    )


async def update_human_ratings_for_game(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    ranked: bool,
    game_result: HumanGameResult,
    rating_enabled: bool | None = None,
    now: datetime | None = None,
) -> list[HumanRatingEvent]:
    """Apply the GLOBAL ranked-human update for one Humans-Included game.

    AI seats participate as transient initial ratings so the team update reflects
    the full mixed game, but only human principal rows are persisted.
    """
    flag_enabled = (
        ranked_human_rating_enabled() if rating_enabled is None else rating_enabled
    ) is True
    _now = now if now is not None else datetime.now(UTC)
    if not flag_enabled:
        await _record_ranked_disabled_refusal(
            session,
            league_id=league_id,
            ranked=ranked,
            game_id=game_result.game_id,
            now=_now,
        )
        return []

    metadata = await _resolve_ranked_humans_included_metadata(
        session,
        league_id=league_id,
        ranked=ranked,
        game_id=game_result.game_id,
    )
    if metadata is None:
        return []

    seats = await _seat_rows(session, game_result.game_id)
    model = PlackettLuce(mu=INITIAL_MU, sigma=INITIAL_SIGMA)
    teams = await _team_entries(
        session,
        league_id=league_id,
        game_id=game_result.game_id,
        seats=seats,
        model=model,
    )
    if teams is None:
        return []
    town_entries, mafia_entries = teams

    ranks = canonical_team_ranks_for_outcome(game_result.winner)
    new_town, new_mafia = model.rate(
        [[entry.rating for entry in town_entries], [entry.rating for entry in mafia_entries]],
        ranks=[ranks[Faction.TOWN.value], ranks[Faction.MAFIA.value]],
    )

    events: list[HumanRatingEvent] = []
    for entry, new in zip(town_entries, new_town, strict=True):
        event = await _persist_human_entry(
            session,
            league_id=league_id,
            game_id=game_result.game_id,
            entry=entry,
            new_mu=entry.row.mu
            if entry.row is not None and game_result.winner == "DRAW"
            else new.mu,
            new_sigma=new.sigma,
            now=_now,
        )
        if event is not None:
            events.append(event)
    for entry, new in zip(mafia_entries, new_mafia, strict=True):
        event = await _persist_human_entry(
            session,
            league_id=league_id,
            game_id=game_result.game_id,
            entry=entry,
            new_mu=entry.row.mu
            if entry.row is not None and game_result.winner == "DRAW"
            else new.mu,
            new_sigma=new.sigma,
            now=_now,
        )
        if event is not None:
            events.append(event)
    return events


__all__ = [
    "SCOPE_VALUE_GLOBAL",
    "FirewallViolation",
    "HumanGameResult",
    "ranked_human_rating_enabled",
    "update_human_ratings_for_game",
]
