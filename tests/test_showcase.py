"""Local showcase lifecycle, routing, anonymity, and scientific-rating firewall."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import litellm
import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from padrino.cli import app
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.enums import Faction, Role
from padrino.core.observations import Observation
from padrino.core.rulesets import mini7_v1
from padrino.db.base import create_engine, create_session_factory
from padrino.db.game_status import GAME_STATUS_COMPLETED, GAME_STATUS_FAILED
from padrino.db.models import (
    AgentBuild,
    Game,
    GameEvent,
    GameSeat,
    LlmCall,
    ModelConfig,
    ModelProvider,
    Rating,
    RatingEvent,
)
from padrino.llm.adapter import AdapterResult, LlmAdapter
from padrino.llm.adapter import AgentBuild as RuntimeBuild
from padrino.llm.mock import DeterministicMockAdapter
from padrino.llm.prompts import canonical_prompts_by_role
from padrino.showcase import PREFLIGHT_PROMPT, ShowcaseError, _showcase_prompts, run_showcase
from tests.conftest import make_town_win_script

SLUGS = ["gemini/gemini-3.8-flash", "cx/gpt-6-luna", "minimax/MiniMax-M3", "cc/claude-sonnet-5-5"]
ROSTER = (SLUGS * 2)[:7]
SEED = "showcase-test"


def _scripted_factory(_assignments: Mapping[str, RuntimeBuild]) -> LlmAdapter:
    seats = assign_roles(SEED, mini7_v1)
    return DeterministicMockAdapter(
        make_town_win_script(
            mafia_ids=[s.public_player_id for s in seats if s.faction is Faction.MAFIA],
            town_ids=[s.public_player_id for s in seats if s.faction is Faction.TOWN],
            doctor_id=next(s.public_player_id for s in seats if s.role is Role.DOCTOR),
            detective_id=next(s.public_player_id for s in seats if s.role is Role.DETECTIVE),
        )
    )


@pytest.mark.parametrize("count", [1, 6, 8])
def test_wrong_slug_count_rejected(count: int, tmp_path: Path) -> None:
    path = tmp_path / "unused.db"
    result = CliRunner().invoke(
        app, ["showcase", *([SLUGS[0]] * count), "--db-url", f"sqlite+aiosqlite:///{path}"]
    )
    assert result.exit_code != 0
    assert "exactly 7" in result.output
    assert not path.exists()


def test_cli_reports_sanitized_execution_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failed_run(*args: Any, **kwargs: Any) -> Any:
        raise ShowcaseError("Showcase execution failed")

    monkeypatch.setattr("padrino.showcase.run_showcase", failed_run)
    result = CliRunner().invoke(app, ["showcase", *ROSTER])
    assert result.exit_code == 1
    assert "Showcase execution failed" in result.output


def test_non_sqlite_rejected() -> None:
    result = CliRunner().invoke(
        app, ["showcase", *ROSTER, "--db-url", "postgresql+asyncpg://localhost/showcase"]
    )
    assert result.exit_code != 0
    assert "SQLite" in result.output


async def test_showcase_lifecycle_and_idempotence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'showcase.db'}"
    engine = create_engine(db_url)
    session_factory = create_session_factory(engine)
    first_call_checked = False

    class WatchingAdapter:
        async def complete(self, observation: Observation) -> AdapterResult:
            nonlocal first_call_checked
            if not first_call_checked:
                first_call_checked = True
                async with session_factory() as session:
                    game = (await session.execute(select(Game))).scalar_one()
                    assert game.broadcast_state == "LIVE"
                    assert game.is_broadcastable
                    assert game.identity_mode == "ANONYMOUS"
                    assert game.gauntlet_id is None
                    assert await session.scalar(select(func.count()).select_from(GameSeat)) == 7
                    assert (
                        f"http://localhost:5173/watch/{game.id}?tail=1&narrated=1"
                        in capsys.readouterr().out
                    )
            assert "gemini" not in observation.model_dump_json()
            return await scripted.complete(observation)

    def factory(assignments: Mapping[str, RuntimeBuild]) -> LlmAdapter:
        assert [b.model_id for b in assignments.values()] == ["openai/" + slug for slug in ROSTER]
        assert all(b.base_url == "http://127.0.0.1:20128/v1" for b in assignments.values())
        assert all(b.auth_secret_ref == "env:NINEROUTER_API_KEY" for b in assignments.values())
        return WatchingAdapter()

    scripted = _scripted_factory({})
    try:
        first = await run_showcase(ROSTER, db_url=db_url, seed=SEED, adapter_factory=factory)
        output = capsys.readouterr().out
        assert first_call_checked
        assert f"http://localhost:5173/watch/{first['game_id']}?narrated=1" in output
        assert json.loads(output.splitlines()[-1]) == first
        assert first["winner"] == "TOWN"
        assert first["calls"] > 0
        async with session_factory() as session:
            first_build_ids = set((await session.execute(select(AgentBuild.id))).scalars())
        second = await run_showcase(
            ROSTER, db_url=db_url, seed=SEED, adapter_factory=_scripted_factory
        )
        assert second["game_id"] != first["game_id"]
        async with session_factory() as session:
            assert set((await session.execute(select(AgentBuild.id))).scalars()) == first_build_ids
            assert len(first_build_ids) == len(set(ROSTER))
            assert await session.scalar(select(func.count()).select_from(ModelProvider)) == 1
            assert await session.scalar(select(func.count()).select_from(ModelConfig)) == len(
                set(ROSTER)
            )
            for game in (await session.execute(select(Game))).scalars():
                assert game.status == GAME_STATUS_COMPLETED
                assert game.broadcast_state == "RECENT"
                assert game.is_broadcastable
                assert game.identity_mode == "ANONYMOUS"
            assert await session.scalar(select(func.count()).select_from(GameSeat)) == 14
            assert (
                await session.execute(select(func.count()).select_from(LlmCall))
            ).scalar_one() == first["calls"] + second["calls"]
            assert await session.scalar(select(func.count()).select_from(Rating)) == 0
            assert await session.scalar(select(func.count()).select_from(RatingEvent)) == 0
        async with engine.connect() as connection:
            assert (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar_one() == "wal"
    finally:
        await engine.dispose()


async def test_default_factory_routes_through_9router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NINEROUTER_API_KEY", "test-key-never-persist")
    scripted = _scripted_factory({})
    requested: set[str] = set()
    preflighted: list[str] = []
    systems: set[str] = set()

    async def completion(**kwargs: Any) -> Any:
        assert kwargs["api_base"] == "http://127.0.0.1:20128/v1"
        assert kwargs["api_key"] == "test-key-never-persist"
        assert kwargs["extra_headers"] == {"x-9router-token-saver": "off"}
        requested.add(kwargs["model"])
        if kwargs["messages"][1]["content"] == PREFLIGHT_PROMPT:
            preflighted.append(kwargs["model"])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])
        systems.add(kwargs["messages"][0]["content"])
        observation = Observation.model_validate_json(kwargs["messages"][1]["content"])
        result = await scripted.complete(observation)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=result.parsed_response.model_dump_json())
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
            _hidden_params={"response_cost": 0.01},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    result = await run_showcase(
        ROSTER, db_url=f"sqlite+aiosqlite:///{tmp_path / 'routing.db'}", seed=SEED
    )
    assert requested == {"openai/" + slug for slug in ROSTER}
    assert sorted(preflighted) == sorted("openai/" + slug for slug in set(ROSTER))
    assert systems
    assert all("in English" in prompt for prompt in systems)
    assert result["winner"] == "TOWN"
    assert result["cost"] == pytest.approx(result["calls"] * 0.01)
    assert result["unpriced_calls"] == 0
    assert "test-key-never-persist" not in capsys.readouterr().out


async def test_adapter_exception_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'failed.db'}"

    class BrokenAdapter:
        async def complete(self, observation: Observation) -> AdapterResult:
            raise RuntimeError("Bearer secret-must-never-be-printed")

    with pytest.raises(ShowcaseError) as error:
        await run_showcase(
            ROSTER, db_url=db_url, seed=SEED, adapter_factory=lambda _: BrokenAdapter()
        )
    assert "secret-must-never-be-printed" not in str(error.value) + capsys.readouterr().out
    engine = create_engine(db_url)
    try:
        async with create_session_factory(engine)() as session:
            game = (await session.execute(select(Game))).scalar_one()
            assert game.status == GAME_STATUS_FAILED
            assert not game.is_broadcastable
            assert game.broadcast_state == "HIDDEN"
            assert game.terminal_result is None
            assert (
                await session.execute(select(func.count()).select_from(GameEvent))
            ).scalar_one() > 0
            assert "secret-must-never-be-printed" not in (game.last_error or "")
            assert await session.scalar(select(func.count()).select_from(Rating)) == 0
            assert await session.scalar(select(func.count()).select_from(RatingEvent)) == 0
    finally:
        await engine.dispose()


async def test_preflight_probes_each_distinct_slug_concurrently(tmp_path: Path) -> None:
    seen: list[str] = []
    gate = asyncio.Event()

    async def preflight(slug: str) -> None:
        seen.append(slug)
        if len(seen) == len(set(ROSTER)):
            gate.set()
        await asyncio.wait_for(gate.wait(), 1)  # deadlocks unless calls overlap

    await run_showcase(
        ROSTER,
        db_url=f"sqlite+aiosqlite:///{tmp_path / 'ok.db'}",
        seed=SEED,
        adapter_factory=_scripted_factory,
        preflight=preflight,
    )
    assert sorted(seen) == sorted(set(ROSTER))


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_preflight_failure_refuses_before_game_creation(
    failure: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "refused.db"
    bad = SLUGS[0]

    async def preflight(slug: str) -> None:
        if slug != bad:
            return
        if failure == "timeout":
            await asyncio.sleep(10)
        raise RuntimeError("Bearer secret-must-never-be-printed")

    def never(_: Mapping[str, RuntimeBuild]) -> LlmAdapter:
        raise AssertionError("game must not start")

    async def fast_timeout(slug: str) -> None:
        await asyncio.wait_for(preflight(slug), 0.05)

    with pytest.raises(ShowcaseError) as error:
        await run_showcase(
            ROSTER,
            db_url=f"sqlite+aiosqlite:///{path}",
            seed=SEED,
            adapter_factory=never,
            preflight=fast_timeout if failure == "timeout" else preflight,
        )
    message = str(error.value)
    assert bad in message
    assert SLUGS[1] not in message
    assert "secret-must-never-be-printed" not in message + capsys.readouterr().out
    assert not path.exists()


async def test_default_preflight_error_exits_nonzero_without_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NINEROUTER_API_KEY", "sk-never-print-me")

    async def completion(**kwargs: Any) -> Any:
        if kwargs["model"].endswith(SLUGS[2]):
            raise RuntimeError("401 sk-never-print-me")
        return SimpleNamespace(choices=[])

    monkeypatch.setattr(litellm, "acompletion", completion)
    path = tmp_path / "cli.db"
    result = await asyncio.to_thread(
        CliRunner().invoke, app, ["showcase", *ROSTER, "--db-url", f"sqlite+aiosqlite:///{path}"]
    )
    assert result.exit_code == 1
    assert SLUGS[2] in result.output
    assert "sk-never-print-me" not in result.output
    assert not path.exists()


def test_language_rule_is_showcase_only() -> None:
    canonical = canonical_prompts_by_role("mini7_v1")
    english = _showcase_prompts("English")
    assert all(
        english[r].startswith(canonical[r]) and "in English" in english[r] for r in canonical
    )
    assert _showcase_prompts(None) == canonical
    assert "in Spanish" in _showcase_prompts("Spanish")[Role.VILLAGER]


@pytest.mark.parametrize(
    ("flag", "expected"), [(None, "English"), ("none", None), ("French", "French")]
)
def test_cli_language_option(
    flag: str | None, expected: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    got: dict[str, Any] = {}

    async def fake(*args: Any, **kwargs: Any) -> None:
        got.update(kwargs)

    monkeypatch.setattr("padrino.showcase.run_showcase", fake)
    result = CliRunner().invoke(app, ["showcase", *ROSTER, *(["--language", flag] if flag else [])])
    assert result.exit_code == 0
    assert got["language"] == expected


@pytest.mark.integration
@pytest.mark.live_llm
@pytest.mark.skipif(not os.environ.get("NINEROUTER_API_KEY"), reason="NINEROUTER_API_KEY is unset")
async def test_real_9router_game(tmp_path: Path) -> None:
    result = await run_showcase(
        ROSTER, db_url=f"sqlite+aiosqlite:///{tmp_path / 'live.db'}", seed=SEED
    )
    assert uuid.UUID(result["game_id"])
    assert result["winner"] in {"TOWN", "MAFIA", "DRAW"}
    assert result["calls"] > 0


async def test_summary_reports_priced_cost_and_unpriced_call_count(tmp_path: Path) -> None:
    """Unknown models without a provider cost are counted, never summed as a made-up total."""
    scripted = _scripted_factory({})

    class TokensNoCost:
        async def complete(self, observation: Observation) -> AdapterResult:
            result = await scripted.complete(observation)
            return result.model_copy(
                update={"input_tokens": 10, "output_tokens": 5, "model_id": "openai/mystery"}
            )

    summary = await run_showcase(
        ROSTER,
        db_url=f"sqlite+aiosqlite:///{tmp_path / 'unpriced.db'}",
        seed=SEED,
        adapter_factory=lambda _: TokensNoCost(),
    )
    assert summary["calls"] > 0
    assert summary["unpriced_calls"] == summary["calls"]
    assert summary["cost"] is None


async def test_unresponsive_seat_fails_the_game_naming_only_the_seat_number(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'afk.db'}"
    scripted = _scripted_factory({})

    class SeatThreeNeverAnswers:
        async def complete(self, observation: Observation) -> AdapterResult:
            result = await scripted.complete(observation)
            if observation.you.player_id != "P03":
                return result
            return result.model_copy(
                update={
                    "status": "exhausted",
                    "failure_reason": "TIMEOUT",
                    "error": "Timeout: gemini/gemini-3.8-flash took too long",
                }
            )

    with pytest.raises(ShowcaseError) as error:
        await run_showcase(
            ROSTER, db_url=db_url, seed=SEED, adapter_factory=lambda _: SeatThreeNeverAnswers()
        )
    message = str(error.value) + capsys.readouterr().out
    assert "seat P03 stopped responding" in message
    assert "gemini" not in message
    engine = create_engine(db_url)
    try:
        async with create_session_factory(engine)() as session:
            game = (await session.execute(select(Game))).scalar_one()
            assert game.status == GAME_STATUS_FAILED
            assert game.terminal_result is None
            assert game.last_error == "seat P03 stopped responding"
            assert game.last_error_kind == "seat_unresponsive"
            assert not game.is_broadcastable
    finally:
        await engine.dispose()
