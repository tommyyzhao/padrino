"""US-141: identity-mode-aware observation + projection.

A playing seat's view (and the spectator/public projection) may reveal
model/human identity ONLY in TRANSPARENT mode, and NEVER in ANONYMOUS mode.
A missing / ``None`` mode coerces to ANONYMOUS (fail closed).

Covers:
- ANONYMOUS: a playing seat's view has ZERO model/provider/agent-build
  identifiers AND zero human-vs-AI markers;
- TRANSPARENT: the view may surface those identifiers while still hiding OTHER
  seats' roles/factions;
- None coerces to anonymous;
- the spectator/public projection is likewise mode-aware.
"""

from __future__ import annotations

from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import Faction, IdentityMode, PhaseKind, Role, SeatKind
from padrino.core.observation_privacy import (
    IDENTITY_MARKER_KEYS,
    assert_no_identity_markers,
)
from padrino.core.observations import (
    SeatIdentity,
    build_observation_for_mode,
)
from padrino.core.rulesets import mini7_v1
from padrino.core.spectator_projection import project_events_for_spectator_mode


def _seven_seats() -> tuple[Seat, ...]:
    spec: list[tuple[str, int, Role, Faction, SeatKind]] = [
        ("P01", 0, Role.MAFIA_GOON, Faction.MAFIA, SeatKind.AI),
        ("P02", 1, Role.MAFIA_GOON, Faction.MAFIA, SeatKind.AI),
        ("P03", 2, Role.DETECTIVE, Faction.TOWN, SeatKind.HUMAN),
        ("P04", 3, Role.DOCTOR, Faction.TOWN, SeatKind.AI),
        ("P05", 4, Role.VILLAGER, Faction.TOWN, SeatKind.HUMAN),
        ("P06", 5, Role.VILLAGER, Faction.TOWN, SeatKind.AI),
        ("P07", 6, Role.VILLAGER, Faction.TOWN, SeatKind.AI),
    ]
    return tuple(
        Seat(
            public_player_id=public_id,
            seat_index=idx,
            role=role,
            faction=faction,
            alive=True,
            seat_kind=kind,
        )
        for public_id, idx, role, faction, kind in spec
    )


def _state() -> GameState:
    return GameState(
        ruleset_id="mini7_v1",
        game_id="G-mode-test",
        game_seed="seed-mode",
        current_phase=Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1),
        seats=_seven_seats(),
        day=1,
    )


def _identities() -> tuple[SeatIdentity, ...]:
    return tuple(
        SeatIdentity(
            public_player_id=s.public_player_id,
            is_human=s.seat_kind is SeatKind.HUMAN,
            seat_kind=s.seat_kind.value if s.seat_kind is not None else None,
            model_id=None if s.seat_kind is SeatKind.HUMAN else "cerebras/zai-glm-4.7",
            provider=None if s.seat_kind is SeatKind.HUMAN else "cerebras",
            agent_build_id=None if s.seat_kind is SeatKind.HUMAN else f"build-{s.public_player_id}",
        )
        for s in _seven_seats()
    )


# --------------------------------------------------------------------------- #
# ANONYMOUS: leaks nothing
# --------------------------------------------------------------------------- #


def test_anonymous_observation_has_no_identity_disclosure() -> None:
    state = _state()
    obs = build_observation_for_mode(
        state,
        state.seats[4],
        EventLog(),
        mini7_v1,
        identity_mode=IdentityMode.ANONYMOUS,
        seat_identities=_identities(),
    )
    assert obs.identity_disclosure is None


def test_anonymous_observation_frame_carries_no_identity_markers() -> None:
    state = _state()
    obs = build_observation_for_mode(
        state,
        state.seats[2],
        EventLog(),
        mini7_v1,
        identity_mode=IdentityMode.ANONYMOUS,
        seat_identities=_identities(),
    )
    frame = obs.model_dump(mode="json")
    # The seat's own role/faction is allowed; identity markers are not.
    assert_no_identity_markers(frame)


def test_none_mode_coerces_to_anonymous() -> None:
    state = _state()
    obs = build_observation_for_mode(
        state,
        state.seats[4],
        EventLog(),
        mini7_v1,
        identity_mode=None,
        seat_identities=_identities(),
    )
    assert obs.identity_disclosure is None
    assert_no_identity_markers(obs.model_dump(mode="json"))


def test_unknown_mode_coerces_to_anonymous() -> None:
    state = _state()
    obs = build_observation_for_mode(
        state,
        state.seats[4],
        EventLog(),
        mini7_v1,
        identity_mode="WHATEVER",
        seat_identities=_identities(),
    )
    assert obs.identity_disclosure is None


# --------------------------------------------------------------------------- #
# TRANSPARENT: surfaces allowed fields, still hides others' roles
# --------------------------------------------------------------------------- #


def test_transparent_observation_surfaces_identity_disclosure() -> None:
    state = _state()
    obs = build_observation_for_mode(
        state,
        state.seats[4],
        EventLog(),
        mini7_v1,
        identity_mode=IdentityMode.TRANSPARENT,
        seat_identities=_identities(),
    )
    assert obs.identity_disclosure is not None
    by_id = {d.public_player_id: d for d in obs.identity_disclosure}
    assert by_id["P03"].is_human is True
    assert by_id["P03"].model_id is None
    assert by_id["P01"].is_human is False
    assert by_id["P01"].model_id == "cerebras/zai-glm-4.7"
    assert by_id["P01"].provider == "cerebras"
    assert by_id["P01"].agent_build_id == "build-P01"


def test_transparent_disclosure_never_carries_other_seats_roles() -> None:
    state = _state()
    # The viewer is a town villager (P05). The disclosure block must not reveal
    # any OTHER seat's role/faction (only model/human identity).
    obs = build_observation_for_mode(
        state,
        state.seats[4],
        EventLog(),
        mini7_v1,
        identity_mode=IdentityMode.TRANSPARENT,
        seat_identities=_identities(),
    )
    assert obs.identity_disclosure is not None
    disclosure_json = [d.model_dump(mode="json") for d in obs.identity_disclosure]
    for entry in disclosure_json:
        assert "role" not in entry
        assert "faction" not in entry
    # The viewer still only sees its OWN role at the top level.
    assert obs.you.role is Role.VILLAGER
    # No mafia teammate leak for a town seat.
    assert obs.mafia_teammates is None


def test_transparent_without_identities_yields_empty_disclosure() -> None:
    state = _state()
    obs = build_observation_for_mode(
        state,
        state.seats[4],
        EventLog(),
        mini7_v1,
        identity_mode=IdentityMode.TRANSPARENT,
        seat_identities=None,
    )
    assert obs.identity_disclosure == ()


# --------------------------------------------------------------------------- #
# Base observation is unchanged in anonymous mode
# --------------------------------------------------------------------------- #


def test_anonymous_matches_base_builder_fields() -> None:
    from padrino.core.observations import build_observation

    state = _state()
    base = build_observation(state, state.seats[4], EventLog(), mini7_v1)
    mode = build_observation_for_mode(
        state,
        state.seats[4],
        EventLog(),
        mini7_v1,
        identity_mode=IdentityMode.ANONYMOUS,
        seat_identities=_identities(),
    )
    base_dump = base.model_dump(mode="json")
    mode_dump = mode.model_dump(mode="json")
    mode_dump.pop("identity_disclosure", None)
    base_dump.pop("identity_disclosure", None)
    assert mode_dump == base_dump


# --------------------------------------------------------------------------- #
# Spectator / public projection is mode-aware
# --------------------------------------------------------------------------- #


def _public_events_with_identity() -> list[dict[str, object]]:
    return [
        {
            "sequence": 1,
            "visibility": "PUBLIC",
            "event_type": "PublicMessageSubmitted",
            "phase": "DAY_1_DISCUSSION_ROUND_1",
            "actor_player_id": "P01",
            "payload": {
                "content_ref": "abc",
                "model_id": "cerebras/zai-glm-4.7",
                "is_human": False,
            },
        }
    ]


def test_spectator_projection_anonymous_strips_identity() -> None:
    projected = project_events_for_spectator_mode(
        _public_events_with_identity(),
        identity_mode=IdentityMode.ANONYMOUS,
    )
    assert len(projected) == 1
    payload = projected[0]["payload"]
    for key in IDENTITY_MARKER_KEYS:
        assert key not in payload


def test_spectator_projection_none_mode_strips_identity() -> None:
    projected = project_events_for_spectator_mode(
        _public_events_with_identity(),
        identity_mode=None,
    )
    payload = projected[0]["payload"]
    assert "model_id" not in payload
    assert "is_human" not in payload


def test_spectator_projection_transparent_keeps_model_identity() -> None:
    projected = project_events_for_spectator_mode(
        _public_events_with_identity(),
        identity_mode=IdentityMode.TRANSPARENT,
    )
    payload = projected[0]["payload"]
    assert payload["model_id"] == "cerebras/zai-glm-4.7"
    assert payload["is_human"] is False


def test_spectator_projection_transparent_still_hides_roles() -> None:
    events = [
        {
            "sequence": 2,
            "visibility": "PUBLIC",
            "event_type": "PlayerEliminated",
            "phase": "DAY_1_VOTE",
            "actor_player_id": None,
            "payload": {
                "public_player_id": "P02",
                "cause": "LYNCH",
                "role": "MAFIA_GOON",
                "faction": "MAFIA",
                "model_id": "cerebras/zai-glm-4.7",
            },
        }
    ]
    projected = project_events_for_spectator_mode(events, identity_mode=IdentityMode.TRANSPARENT)
    payload = projected[0]["payload"]
    # Model identity surfaces in transparent mode...
    assert payload["model_id"] == "cerebras/zai-glm-4.7"
    # ...but a pre-reveal role/faction never does.
    assert "role" not in payload
    assert "faction" not in payload
