"""Pure ranked human-rating rollback recompute helpers.

The impure shell supplies ordered game projections and any wall-clock/database
facts. This module only replays deterministic OpenSkill team updates, skipping
excluded games and treating seats without a historical human rating as transient
initial ratings. AI takeover seats are also transient team placeholders, with
the abandoned target human rated separately as a deterministic forfeit loss.
"""

from __future__ import annotations

from collections.abc import Iterable, Set
from dataclasses import dataclass
from typing import Any, Literal

from openskill.models import PlackettLuce

FactionValue = Literal["TOWN", "MAFIA"]
WinnerValue = Literal["TOWN", "MAFIA", "DRAW"]

INITIAL_MU: float = 25.0
INITIAL_SIGMA: float = 25.0 / 3.0
SEAT_KIND_AI_TAKEOVER: str = "AI_TAKEOVER"


@dataclass(frozen=True, slots=True)
class RatingRollbackPlayer:
    """One seat projection used to recompute a ranked human-rating game."""

    public_player_id: str
    faction: FactionValue
    seat_kind: str = "AI"
    human_player_id: str | None = None
    before_mu: float = INITIAL_MU
    before_sigma: float = INITIAL_SIGMA


@dataclass(frozen=True, slots=True)
class RatingRollbackGame:
    """One ordered game projection in a human-rating history."""

    game_id: str
    winner: WinnerValue
    players: tuple[RatingRollbackPlayer, ...]


@dataclass(frozen=True, slots=True)
class RatingRollbackResult:
    """Final rating state after replaying a filtered ranked history."""

    mu: float
    sigma: float
    conservative_score: float
    games: int


def recompute_human_rating(
    ordered_games: Iterable[RatingRollbackGame],
    *,
    human_player_id: str,
    excluded_game_ids: Set[str],
    initial_mu: float = INITIAL_MU,
    initial_sigma: float = INITIAL_SIGMA,
) -> RatingRollbackResult:
    """Replay a principal's rating history with selected games excluded.

    The target human's rating is threaded through the supplied game order. Other
    humans use their per-game historical ``before_*`` values, and AI seats
    (``human_player_id is None``) are reconstructed as transient initial ratings.
    """

    model = PlackettLuce(mu=initial_mu, sigma=initial_sigma)
    current_mu = initial_mu
    current_sigma = initial_sigma
    games = 0
    for game in ordered_games:
        if game.game_id in excluded_game_ids:
            continue
        next_rating = _rate_one_game(
            model,
            game,
            human_player_id=human_player_id,
            current_mu=current_mu,
            current_sigma=current_sigma,
            initial_mu=initial_mu,
            initial_sigma=initial_sigma,
        )
        if next_rating is None:
            continue
        current_mu, current_sigma = next_rating
        games += 1
    return RatingRollbackResult(
        mu=current_mu,
        sigma=current_sigma,
        conservative_score=_conservative(current_mu, current_sigma),
        games=games,
    )


def _rate_one_game(
    model: PlackettLuce,
    game: RatingRollbackGame,
    *,
    human_player_id: str,
    current_mu: float,
    current_sigma: float,
    initial_mu: float,
    initial_sigma: float,
) -> tuple[float, float] | None:
    town: list[Any] = []
    mafia: list[Any] = []
    target_rating: Any | None = None
    target_faction: FactionValue | None = None
    target_is_takeover = False
    for player in game.players:
        is_takeover = player.seat_kind == SEAT_KIND_AI_TAKEOVER
        if player.human_player_id == human_player_id:
            target_is_takeover = is_takeover
            target_rating = model.create_rating([current_mu, current_sigma])
            target_faction = player.faction
            rating = (
                model.create_rating([initial_mu, initial_sigma]) if is_takeover else target_rating
            )
        elif is_takeover or player.human_player_id is None:
            rating = model.create_rating([initial_mu, initial_sigma])
        else:
            rating = model.create_rating([player.before_mu, player.before_sigma])

        if player.faction == "TOWN":
            town.append(rating)
        elif player.faction == "MAFIA":
            mafia.append(rating)
        else:
            raise ValueError(f"unsupported faction for rollback: {player.faction!r}")

    if target_rating is None or target_faction is None or not town or not mafia:
        return None

    if target_is_takeover:
        new_rating = _rate_forfeit_loss(
            model,
            target_rating,
            initial_mu=initial_mu,
            initial_sigma=initial_sigma,
        )
        return new_rating.mu, new_rating.sigma

    ranks = _ranks_for_winner(game.winner)
    new_town, new_mafia = model.rate(
        [town, mafia],
        ranks=[ranks["TOWN"], ranks["MAFIA"]],
    )
    rated_team = new_town if target_faction == "TOWN" else new_mafia
    original_team = town if target_faction == "TOWN" else mafia
    target_index = original_team.index(target_rating)
    new_rating = rated_team[target_index]
    next_mu = current_mu if game.winner == "DRAW" else new_rating.mu
    return next_mu, new_rating.sigma


def _rate_forfeit_loss(
    model: PlackettLuce,
    target_rating: Any,
    *,
    initial_mu: float,
    initial_sigma: float,
) -> Any:
    """Return the abandoned human's rating after one deterministic loss."""
    _winner, forfeiter = model.rate(
        [[model.create_rating([initial_mu, initial_sigma])], [target_rating]],
        ranks=[0, 1],
    )
    return forfeiter[0]


def _ranks_for_winner(winner: WinnerValue) -> dict[FactionValue, int]:
    if winner == "TOWN":
        return {"TOWN": 1, "MAFIA": 2}
    if winner == "MAFIA":
        return {"TOWN": 2, "MAFIA": 1}
    if winner == "DRAW":
        return {"TOWN": 1, "MAFIA": 1}
    raise ValueError(f"unsupported rollback winner: {winner!r}")


def _conservative(mu: float, sigma: float) -> float:
    return mu - 3.0 * sigma


__all__ = [
    "INITIAL_MU",
    "INITIAL_SIGMA",
    "SEAT_KIND_AI_TAKEOVER",
    "RatingRollbackGame",
    "RatingRollbackPlayer",
    "RatingRollbackResult",
    "recompute_human_rating",
]
