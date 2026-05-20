"""US-077: gauntlet evaluation report tests.

Builds a fixture gauntlet with three scripted games (TOWN, MAFIA, DRAW
outcomes) and asserts the :class:`GauntletReport` shape, the Wilson CI
math against hand-computed values, the role-family breakdown, the rating
deltas, and the public-projection redaction.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.core.engine.hashing import GENESIS_HASH, compute_event_hash
from padrino.core.enums import Faction, RoleFamily
from padrino.core.rulesets import mini7_v1
from padrino.core.statistics import wilson_score_interval
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.repositories import (
    agent_builds as agent_builds_repo,
)
from padrino.db.repositories import (
    events as events_repo,
)
from padrino.db.repositories import (
    games as games_repo,
)
from padrino.db.repositories import (
    leagues as leagues_repo,
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
from padrino.db.repositories import (
    ratings as ratings_repo,
)
from padrino.gauntlets.completion import finalize_gauntlet_if_done
from padrino.gauntlets.evaluation import (
    GauntletReport,
    evaluate_gauntlet,
    redact_for_public,
)
from padrino.gauntlets.scheduler import create_gauntlet
from padrino.ratings.openskill_service import (
    INITIAL_MU,
    INITIAL_SIGMA,
    SCOPE_FACTION,
    SCOPE_GLOBAL,
    SCOPE_VALUE_GLOBAL,
)

# 7 seats: slots 0,1 are mafia, slot 2 is detective, slot 3 is doctor,
# slots 4,5,6 are villagers. Mirrors the mini7_v1 role mix.
_ROLES_BY_SLOT = (
    ("MAFIA_GOON", Faction.MAFIA),
    ("MAFIA_GOON", Faction.MAFIA),
    ("DETECTIVE", Faction.TOWN),
    ("DOCTOR", Faction.TOWN),
    ("VILLAGER", Faction.TOWN),
    ("VILLAGER", Faction.TOWN),
    ("VILLAGER", Faction.TOWN),
)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


async def _seed_world(
    session: AsyncSession,
    *,
    ph: str = "eval",
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    provider = await providers_repo.create(
        session, name="cerebras", auth_secret_ref="CEREBRAS_API_KEY"
    )
    mc = await model_configs_repo.create(
        session,
        provider_id=provider.id,
        model_name="glm-4.7",
        default_temperature=0.7,
        default_top_p=1.0,
        default_max_output_tokens=4096,
        supports_structured_outputs=True,
    )
    pv = await prompt_versions_repo.create(
        session,
        ruleset_id=mini7_v1.RULESET_ID,
        version="v1",
        system_prompt="sys",
        developer_prompt="dev",
        response_schema={"type": "object"},
        prompt_hash=f"{ph}-{uuid.uuid4().hex}",
    )
    league = await leagues_repo.create(
        session, name="L", ruleset_id=mini7_v1.RULESET_ID, ranked=True
    )
    roster: list[uuid.UUID] = []
    for i in range(mini7_v1.PLAYER_COUNT):
        ab = await agent_builds_repo.create(
            session,
            display_name=f"seat-{i}",
            model_config_id=mc.id,
            prompt_version_id=pv.id,
            adapter_version="2026.05",
            inference_params={},
            active=True,
        )
        roster.append(ab.id)
    return league.id, pv.id, roster


async def _append_chained(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    bodies: list[dict[str, Any]],
) -> None:
    prev = GENESIS_HASH
    for i, body in enumerate(bodies):
        sealed = dict(body)
        sealed["sequence"] = i
        ev_hash = compute_event_hash(prev, sealed)
        await events_repo.append_event(
            session,
            game_id=game_id,
            sequence=sealed["sequence"],
            event_type=sealed["event_type"],
            phase=sealed["phase"],
            visibility=sealed["visibility"],
            actor_player_id=sealed.get("actor_player_id"),
            payload=dict(sealed.get("payload", {})),
            prev_event_hash=prev,
            event_hash=ev_hash,
        )
        prev = ev_hash


async def _seed_seats_for_game(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    roster: list[uuid.UUID],
) -> None:
    for i, ab_id in enumerate(roster):
        role, faction = _ROLES_BY_SLOT[i]
        sid = f"P{i + 1:02d}"
        await games_repo.add_seat(
            session,
            game_id=game_id,
            public_player_id=sid,
            seat_index=i,
            agent_build_id=ab_id,
            role=role,
            faction=faction.value,
            alive=True,
        )


def _action_events(actor: str, types: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for t in types:
        events.append(
            {
                "event_type": t,
                "phase": "DAY_VOTE" if t == "VoteSubmitted" else "NIGHT_ACTIONS",
                "visibility": "PRIVATE",
                "actor_player_id": actor,
                "payload": {"target": "P03"},
            }
        )
    return events


def _terminate_body(winner: str, reason: str, day: int) -> dict[str, Any]:
    return {
        "event_type": "GameTerminated",
        "phase": "TERMINAL",
        "visibility": "PUBLIC",
        "actor_player_id": None,
        "payload": {"winner": winner, "reason": reason, "day_terminated": day},
    }


async def _terminate_game(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    winner: str,
    reason: str,
    day_terminated: int,
    pre_events: list[dict[str, Any]] | None = None,
) -> None:
    bodies = list(pre_events or [])
    bodies.append(_terminate_body(winner, reason, day_terminated))
    await _append_chained(session, game_id=game_id, bodies=bodies)
    await games_repo.update_status(
        session,
        game_id,
        status="COMPLETED",
        terminal_result={"winner": winner, "reason": reason, "day_terminated": day_terminated},
    )


async def _record_rating_events_for_game(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    game_id: uuid.UUID,
    roster: list[uuid.UUID],
    winner: str,
    pre_mu: dict[uuid.UUID, float] | None = None,
    post_mu_delta: float = 0.5,
) -> dict[uuid.UUID, float]:
    """Synthesize a per-game rating audit row for every roster slot.

    Skips the real PlackettLuce math — just lays down ``RatingEvent`` rows
    with the requested mu drift so the report can read them back. Returns
    the new ``post_mu`` per agent_build so the caller can chain games.
    """
    new_mu: dict[uuid.UUID, float] = {}
    for i, ab_id in enumerate(roster):
        _role, faction = _ROLES_BY_SLOT[i]
        before = INITIAL_MU if pre_mu is None else pre_mu.get(ab_id, INITIAL_MU)
        if winner == faction.value:
            after = before + post_mu_delta
        elif winner in {Faction.TOWN.value, Faction.MAFIA.value}:
            after = before - post_mu_delta
        else:  # DRAW
            after = before
        new_mu[ab_id] = after
        await ratings_repo.record_rating_event(
            session,
            league_id=league_id,
            game_id=game_id,
            agent_build_id=ab_id,
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            before_mu=before,
            before_sigma=INITIAL_SIGMA,
            after_mu=after,
            after_sigma=INITIAL_SIGMA,
        )
    return new_mu


async def _build_fixture_gauntlet(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ph: str = "eval",
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    """Build a 3-clone gauntlet with one TOWN, one MAFIA, and one DRAW game.

    Returns ``(gauntlet_id, league_id, roster)``. Games are not finalized
    via ``finalize_gauntlet_if_done`` by default — the test that needs
    ``status='COMPLETED'`` triggers that step itself.
    """
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session, ph=ph)

    async with session_factory() as session:
        gauntlet = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=3,
            gauntlet_seed=ph,
            roster=roster,
        )

    # Each game: 2 real action events from one actor + the terminator.
    pre_events_for = [
        _action_events("P01", ["MafiaKillVoteSubmitted", "VoteSubmitted"]),
        _action_events("P02", ["MafiaKillVoteSubmitted"]),
        _action_events("P03", ["InvestigateSubmitted", "ProtectSubmitted", "VoteSubmitted"]),
    ]
    winners = [Faction.TOWN.value, Faction.MAFIA.value, "DRAW"]
    days = [3, 4, 5]
    reasons = [
        "town_eliminated_all_mafia",
        "mafia_outnumber_town",
        "max_days_reached",
    ]

    pre_mu: dict[uuid.UUID, float] = {}
    async with session_factory() as session, session.begin():
        for game_id, winner, day, reason, pre in zip(
            gauntlet.game_ids, winners, days, reasons, pre_events_for, strict=True
        ):
            await _seed_seats_for_game(session, game_id=game_id, roster=roster)
            await _terminate_game(
                session,
                game_id=game_id,
                winner=winner,
                reason=reason,
                day_terminated=day,
                pre_events=pre,
            )
            pre_mu = await _record_rating_events_for_game(
                session,
                league_id=league_id,
                game_id=game_id,
                roster=roster,
                winner=winner,
                pre_mu=pre_mu,
            )
    return gauntlet.gauntlet_id, league_id, roster


async def test_evaluate_gauntlet_returns_none_for_unknown_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        report = await evaluate_gauntlet(uuid.uuid4(), session)
    assert report is None


async def test_evaluate_gauntlet_basic_shape(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, _league_id, roster = await _build_fixture_gauntlet(session_factory, ph="basic")

    async with session_factory() as session:
        report = await evaluate_gauntlet(gauntlet_id, session)

    assert report is not None
    assert isinstance(report, GauntletReport)
    assert report.games_total == 3
    assert report.games_completed == 3
    assert report.ruleset_id == mini7_v1.RULESET_ID
    assert report.clone_count == 3
    # One TOWN, one MAFIA, one DRAW outcome was scripted.
    assert report.faction_win_counts == {"TOWN": 1, "MAFIA": 1, "DRAW": 1}
    assert {f.faction for f in report.faction_win_rates} == {"TOWN", "MAFIA", "DRAW"}
    for entry in report.faction_win_rates:
        assert entry.games == 3
        assert entry.wins == 1
        assert 0.0 < entry.rate.lower < entry.rate.upper < 1.0
    # Average days: (3 + 4 + 5) / 3 = 4.0.
    assert report.average_days_to_terminal == pytest.approx(4.0)
    # 2 + 1 + 3 = 6 real-action events across 21 seat rows => 0.2857...
    assert report.average_actions_per_seat == pytest.approx(6.0 / 21.0, rel=1e-9)
    # Rating deltas: one per roster slot, all on GLOBAL/global.
    assert len(report.rating_deltas) == len(roster)
    assert {d.agent_build_id for d in report.rating_deltas} == set(roster)
    for d in report.rating_deltas:
        assert d.scope_type == SCOPE_GLOBAL
        assert d.scope_value == SCOPE_VALUE_GLOBAL
        assert d.games_in_gauntlet == 3
        assert d.pre_mu == pytest.approx(INITIAL_MU)
        # delta_mu is the chained sum of per-game wins/losses for that slot.
        # MAFIA slots: +0.5 (TOWN game = loss for mafia) wait — winner=TOWN means
        # mafia loses, so for mafia slots delta is -0.5 + +0.5 + 0 = 0.0.
        # TOWN slots: +0.5 - 0.5 + 0 = 0.0.
        assert d.delta_mu == pytest.approx(0.0, abs=1e-9)


async def test_faction_win_rate_wilson_ci_is_non_degenerate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """At n=3 the Wilson CI must not collapse to 0..1 — the post-Wave-2 audit gate."""
    gauntlet_id, _league_id, _roster = await _build_fixture_gauntlet(session_factory, ph="ci-band")
    async with session_factory() as session:
        report = await evaluate_gauntlet(gauntlet_id, session)
    assert report is not None
    for entry in report.faction_win_rates:
        assert entry.games >= 3
        # Strictly inside (0, 1) — not the trivial full simplex.
        assert 0.0 < entry.rate.lower < entry.rate.upper < 1.0
        assert entry.rate.upper - entry.rate.lower < 1.0
        # Cross-check against the pure-core helper directly.
        expected = wilson_score_interval(entry.wins, entry.games)
        assert entry.rate.lower == pytest.approx(expected.lower)
        assert entry.rate.upper == pytest.approx(expected.upper)
        assert entry.rate.point == pytest.approx(expected.point)


async def test_role_family_breakdown_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, _league_id, _roster = await _build_fixture_gauntlet(
        session_factory, ph="role-family"
    )
    async with session_factory() as session:
        report = await evaluate_gauntlet(gauntlet_id, session)
    assert report is not None
    by_family = {rf.role_family: rf for rf in report.role_family_breakdown}
    # 4 RoleFamily values are always present.
    assert set(by_family) == {rf.value for rf in RoleFamily}

    # DECEPTIVE = 2 mafia seats x 3 games = 6 seat-games.
    # MAFIA won 1 game -> 2 wins; lost 1 -> 2 losses; 1 draw -> 2 draws.
    deceptive = by_family[RoleFamily.DECEPTIVE.value]
    assert deceptive.games == 6
    assert deceptive.wins == 2
    assert deceptive.losses == 2
    assert deceptive.draws == 2

    # INVESTIGATIVE = 1 detective seat x 3 games = 3 seat-games (town).
    investigative = by_family[RoleFamily.INVESTIGATIVE.value]
    assert investigative.games == 3
    assert investigative.wins == 1  # TOWN game = win
    assert investigative.losses == 1  # MAFIA game
    assert investigative.draws == 1

    # PROTECTIVE = 1 doctor seat x 3 games = 3 seat-games (town).
    protective = by_family[RoleFamily.PROTECTIVE.value]
    assert protective.games == 3
    assert protective.wins == 1

    # VANILLA_TOWN = 3 villager seats x 3 games = 9 seat-games (town).
    villagers = by_family[RoleFamily.VANILLA_TOWN.value]
    assert villagers.games == 9
    assert villagers.wins == 3
    assert villagers.losses == 3
    assert villagers.draws == 3


async def test_redact_for_public_strips_model_identity_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, _league_id, _roster = await _build_fixture_gauntlet(session_factory, ph="redact")
    async with session_factory() as session:
        report = await evaluate_gauntlet(gauntlet_id, session)
    assert report is not None
    redacted = redact_for_public(report)
    # Top-level shape preserved.
    assert redacted["games_total"] == 3
    assert isinstance(redacted["rating_deltas"], list)
    # Every per-build delta exposes ``agent_build_id`` only — never model
    # provider / model_name / model_version / display_name (defense in depth).
    forbidden = {"model_provider", "model_name", "model_version", "provider", "display_name"}
    for entry in redacted["rating_deltas"]:
        assert "agent_build_id" in entry
        assert forbidden.isdisjoint(entry.keys())


async def test_evaluate_gauntlet_partial_when_games_in_flight(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A gauntlet with some games still PENDING reports games_completed < games_total."""
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session, ph="partial")

    async with session_factory() as session:
        gauntlet = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=3,
            gauntlet_seed="partial",
            roster=roster,
        )

    # Terminate only the first child game.
    async with session_factory() as session, session.begin():
        await _seed_seats_for_game(session, game_id=gauntlet.game_ids[0], roster=roster)
        await _terminate_game(
            session,
            game_id=gauntlet.game_ids[0],
            winner=Faction.TOWN.value,
            reason="town_eliminated_all_mafia",
            day_terminated=2,
        )

    async with session_factory() as session:
        report = await evaluate_gauntlet(gauntlet.gauntlet_id, session)
    assert report is not None
    assert report.games_total == 3
    assert report.games_completed == 1
    assert report.faction_win_counts["TOWN"] == 1


async def test_evaluate_gauntlet_status_reflects_finalized(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gauntlet_id, _league_id, _roster = await _build_fixture_gauntlet(session_factory, ph="finalize")
    async with session_factory() as session:
        finalized = await finalize_gauntlet_if_done(session, gauntlet_id)
    assert finalized is not None
    async with session_factory() as session:
        report = await evaluate_gauntlet(gauntlet_id, session)
    assert report is not None
    assert report.status == "COMPLETED"


async def test_rating_deltas_track_per_scope_and_per_build(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sanity-check that a FACTION-scope rating event surfaces as a separate delta."""
    async with session_factory() as session, session.begin():
        league_id, pv_id, roster = await _seed_world(session, ph="scope")

    async with session_factory() as session:
        gauntlet = await create_gauntlet(
            session,
            league_id=league_id,
            ruleset_id=mini7_v1.RULESET_ID,
            prompt_version_id=pv_id,
            clone_count=1,
            gauntlet_seed="scope",
            roster=roster,
        )

    async with session_factory() as session, session.begin():
        gid = gauntlet.game_ids[0]
        await _seed_seats_for_game(session, game_id=gid, roster=roster)
        await _terminate_game(
            session,
            game_id=gid,
            winner=Faction.TOWN.value,
            reason="town_eliminated_all_mafia",
            day_terminated=2,
        )
        # GLOBAL scope.
        await ratings_repo.record_rating_event(
            session,
            league_id=league_id,
            game_id=gid,
            agent_build_id=roster[0],
            scope_type=SCOPE_GLOBAL,
            scope_value=SCOPE_VALUE_GLOBAL,
            before_mu=INITIAL_MU,
            before_sigma=INITIAL_SIGMA,
            after_mu=INITIAL_MU - 0.4,
            after_sigma=INITIAL_SIGMA - 0.1,
        )
        # FACTION/MAFIA scope on the same build.
        await ratings_repo.record_rating_event(
            session,
            league_id=league_id,
            game_id=gid,
            agent_build_id=roster[0],
            scope_type=SCOPE_FACTION,
            scope_value=Faction.MAFIA.value,
            before_mu=INITIAL_MU,
            before_sigma=INITIAL_SIGMA,
            after_mu=INITIAL_MU - 0.2,
            after_sigma=INITIAL_SIGMA,
        )

    async with session_factory() as session:
        report = await evaluate_gauntlet(gauntlet.gauntlet_id, session)
    assert report is not None
    by_key = {(d.agent_build_id, d.scope_type, d.scope_value): d for d in report.rating_deltas}
    assert (roster[0], SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL) in by_key
    assert (roster[0], SCOPE_FACTION, Faction.MAFIA.value) in by_key
    g_delta = by_key[(roster[0], SCOPE_GLOBAL, SCOPE_VALUE_GLOBAL)]
    assert g_delta.delta_mu == pytest.approx(-0.4)
    assert g_delta.delta_sigma == pytest.approx(-0.1)
    f_delta = by_key[(roster[0], SCOPE_FACTION, Faction.MAFIA.value)]
    assert f_delta.delta_mu == pytest.approx(-0.2)
    assert f_delta.delta_sigma == pytest.approx(0.0)
