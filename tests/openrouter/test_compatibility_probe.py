"""US-346 compatibility probe runner coverage."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from padrino.api.app import create_app
from padrino.api.routes.model_catalog import _qualification_for_entry
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import CatalogSnapshot, ModelCatalogEntry, ModelProbeEvidence
from padrino.db.repositories.model_catalog_snapshots import (
    CatalogEntryInput,
    ProbeEvidencePersistenceError,
    create_or_get_completed_snapshot,
    create_or_get_probe_evidence,
)
from padrino.openrouter.compatibility_probe import (
    DEFAULT_PROBE_SUITE_VERSION,
    CompatibilityProbeError,
    CompatibilityProbeRunner,
    DeterministicFakeProbeTransport,
    ProbeRunMode,
    canonical_probe_cases,
)

NOW = datetime(2026, 7, 12, 15, 0, tzinfo=UTC)


def _entry() -> CatalogEntryInput:
    return CatalogEntryInput(
        canonical_slug="openai/gpt-5-mini",
        display_name="GPT-5 Mini",
        description="A compact text model.",
        modalities=("text",),
        context_length=128_000,
        pricing_strings={"prompt": "0.00000025", "completion": "0.000002"},
        pricing_micros={"prompt_per_million": 250_000, "completion_per_million": 2_000_000},
        supported_parameters=("response_format", "temperature"),
        provider_summary={"provider_fingerprint": "provider-v1"},
        expires_at=None,
        raw_metadata={"architecture": {"output_modalities": ["text"]}},
    )


async def _seed_catalog(
    session_factory: async_sessionmaker[AsyncSession], *, content_hash: str
) -> tuple[CatalogSnapshot, ModelCatalogEntry]:
    async with session_factory() as session:
        snapshot, _ = await create_or_get_completed_snapshot(
            session,
            content_hash=content_hash,
            fetched_at=NOW,
            source_etag=None,
            source_version="test-v1",
            entries=[_entry()],
        )
        await session.flush()
        entry = await session.scalar(
            select(ModelCatalogEntry).where(ModelCatalogEntry.catalog_snapshot_id == snapshot.id)
        )
        assert entry is not None
        await session.commit()
        return snapshot, entry


async def test_default_probe_covers_contract_and_persists_safe_append_only_evidence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    snapshot, entry = await _seed_catalog(session_factory, content_hash="a" * 64)
    transport = DeterministicFakeProbeTransport()
    runner = CompatibilityProbeRunner(
        session_factory=session_factory,
        transport=transport,
        now=lambda: NOW,
        trigger_rate_limit_per_minute=10,
        operator_cost_cap_micros=100_000,
    )

    result = await runner.run(
        catalog_content_hash=snapshot.content_hash,
        canonical_slug=entry.canonical_slug,
        mode=ProbeRunMode.FAKE,
        trigger_key="test-admin",
    )

    expected_case_ids = {
        "json_contract",
        "public_message",
        "vote",
        "mafia_kill",
        "protect",
        "investigate",
        "roleblock",
        "frame",
        "track",
        "watch",
        "clean",
        "serial_kill",
    }
    assert {case.case_id for case in canonical_probe_cases()} == expected_case_ids
    assert result.created is True
    assert result.status == "success"
    assert result.evidence.catalog_snapshot_id == snapshot.id
    assert result.evidence.model_catalog_entry_id == entry.id
    assert result.evidence.probe_suite_version == DEFAULT_PROBE_SUITE_VERSION
    assert result.evidence.observed_at == NOW
    assert len(transport.requests) == len(expected_case_ids)

    evidence = result.evidence.evidence
    assert evidence["catalog_content_hash"] == "a" * 64
    assert evidence["schema_coercion_succeeded"] is True
    assert evidence["strict_schema_observed"] is True
    assert evidence["tokens_used"] > 0
    assert evidence["latency_ms"] > 0
    assert evidence["cost_micros"] == 0
    assert {item["case_id"] for item in evidence["cases"]} == expected_case_ids
    assert all(len(item["result_hash"]) == 64 for item in evidence["cases"])
    assert all("provider" in item and "routing" in item for item in evidence["cases"])
    rendered = json.dumps(evidence, sort_keys=True).lower()
    assert "authorization" not in rendered
    assert "api_key" not in rendered
    assert "raw_response" not in rendered

    async with session_factory() as session:
        rows = list((await session.execute(select(ModelProbeEvidence))).scalars())
    assert [row.id for row in rows] == [result.evidence.id]


async def test_probe_failure_appends_new_evidence_and_demotes_only_derived_eligibility(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_snapshot, first_entry = await _seed_catalog(session_factory, content_hash="b" * 64)
    successful = CompatibilityProbeRunner(
        session_factory=session_factory,
        transport=DeterministicFakeProbeTransport(),
        now=lambda: NOW,
        trigger_rate_limit_per_minute=10,
        operator_cost_cap_micros=100_000,
    )
    original = await successful.run(
        catalog_content_hash=first_snapshot.content_hash,
        canonical_slug=first_entry.canonical_slug,
        mode=ProbeRunMode.FAKE,
        trigger_key="test-admin-success",
    )

    second_snapshot, second_entry = await _seed_catalog(session_factory, content_hash="c" * 64)
    failed = CompatibilityProbeRunner(
        session_factory=session_factory,
        transport=DeterministicFakeProbeTransport(failure_case_id="investigate"),
        now=lambda: NOW,
        trigger_rate_limit_per_minute=10,
        operator_cost_cap_micros=100_000,
    )
    result = await failed.run(
        catalog_content_hash=second_snapshot.content_hash,
        canonical_slug=second_entry.canonical_slug,
        mode=ProbeRunMode.FAKE,
        trigger_key="test-admin-failure",
    )

    assert result.status == "failed"
    assert result.evidence.evidence["schema_coercion_succeeded"] is False
    assert result.evidence.evidence["failed_case_ids"] == ["investigate"]
    assert original.evidence.evidence["schema_coercion_succeeded"] is True
    tier, reasons, casual_visible = _qualification_for_entry(
        second_entry,
        probe_records=[result.evidence],
        now=NOW,
    )
    assert tier == "unverified"
    assert "probe_schema_coercion_failed" in reasons
    assert casual_visible is True

    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(ModelProbeEvidence).order_by(ModelProbeEvidence.catalog_snapshot_id)
                )
            ).scalars()
        )
    by_id = {row.id: row for row in rows}
    assert set(by_id) == {original.evidence.id, result.evidence.id}
    assert by_id[original.evidence.id].evidence["schema_coercion_succeeded"] is True
    assert by_id[result.evidence.id].evidence["schema_coercion_succeeded"] is False

    refusal_snapshot, refusal_entry = await _seed_catalog(session_factory, content_hash="4" * 64)
    refusal = CompatibilityProbeRunner(
        session_factory=session_factory,
        transport=DeterministicFakeProbeTransport(
            failure_case_id="public_message",
            failure_status="refusal",
        ),
        now=lambda: NOW,
        trigger_rate_limit_per_minute=10,
        operator_cost_cap_micros=100_000,
    )
    refusal_result = await refusal.run(
        catalog_content_hash=refusal_snapshot.content_hash,
        canonical_slug=refusal_entry.canonical_slug,
        mode=ProbeRunMode.FAKE,
        trigger_key="test-admin-refusal",
    )
    refused_case = next(
        item
        for item in refusal_result.evidence.evidence["cases"]
        if item["case_id"] == "public_message"
    )
    assert refusal_result.status == "failed"
    assert refused_case["status"] == "refusal"
    assert refused_case["passed"] is False


async def test_probe_evidence_rejects_raw_transport_or_credential_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    snapshot, entry = await _seed_catalog(session_factory, content_hash="1" * 64)
    async with session_factory() as session:
        with pytest.raises(ProbeEvidencePersistenceError, match="probe_evidence_forbidden_key"):
            await create_or_get_probe_evidence(
                session,
                catalog_snapshot_id=snapshot.id,
                model_catalog_entry_id=entry.id,
                prompt_hash="2" * 64,
                schema_hash="3" * 64,
                probe_suite_version="probe-test-v1",
                status="failed",
                evidence={"cases": [{"raw_response": "sentinel-credential"}]},
                observed_at=NOW,
            )
        assert list((await session.execute(select(ModelProbeEvidence))).scalars()) == []


async def test_probe_trigger_is_idempotent_under_races_and_live_runs_require_a_spend_cap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    snapshot, entry = await _seed_catalog(session_factory, content_hash="d" * 64)
    transport = DeterministicFakeProbeTransport()
    runner = CompatibilityProbeRunner(
        session_factory=session_factory,
        transport=transport,
        now=lambda: NOW,
        trigger_rate_limit_per_minute=10,
        operator_cost_cap_micros=100_000,
    )

    first, second = await asyncio.gather(
        runner.run(
            catalog_content_hash=snapshot.content_hash,
            canonical_slug=entry.canonical_slug,
            mode=ProbeRunMode.FAKE,
            trigger_key="same-admin",
        ),
        runner.run(
            catalog_content_hash=snapshot.content_hash,
            canonical_slug=entry.canonical_slug,
            mode=ProbeRunMode.FAKE,
            trigger_key="same-admin",
        ),
    )

    assert {first.created, second.created} == {False, True}
    assert first.evidence.id == second.evidence.id
    assert len(transport.requests) == len(canonical_probe_cases())

    with pytest.raises(CompatibilityProbeError, match="probe_spend_cap_required"):
        await runner.run(
            catalog_content_hash=snapshot.content_hash,
            canonical_slug=entry.canonical_slug,
            mode=ProbeRunMode.LIVE,
            trigger_key="live-admin",
        )


async def test_new_probe_triggers_are_rate_limited_and_live_spend_is_operator_bounded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_snapshot, first_entry = await _seed_catalog(session_factory, content_hash="5" * 64)
    limited = CompatibilityProbeRunner(
        session_factory=session_factory,
        transport=DeterministicFakeProbeTransport(),
        now=lambda: NOW,
        monotonic=lambda: 100.0,
        trigger_rate_limit_per_minute=1,
        operator_cost_cap_micros=100_000,
    )
    await limited.run(
        catalog_content_hash=first_snapshot.content_hash,
        canonical_slug=first_entry.canonical_slug,
        mode=ProbeRunMode.FAKE,
        trigger_key="one-admin",
    )
    second_snapshot, second_entry = await _seed_catalog(session_factory, content_hash="6" * 64)
    with pytest.raises(CompatibilityProbeError, match="probe_trigger_rate_limited"):
        await limited.run(
            catalog_content_hash=second_snapshot.content_hash,
            canonical_slug=second_entry.canonical_slug,
            mode=ProbeRunMode.FAKE,
            trigger_key="one-admin",
        )
    with pytest.raises(CompatibilityProbeError, match="probe_spend_cap_exceeds_operator_limit"):
        await limited.run(
            catalog_content_hash=second_snapshot.content_hash,
            canonical_slug=second_entry.canonical_slug,
            mode=ProbeRunMode.LIVE,
            trigger_key="different-admin",
            spend_cap_micros=100_001,
        )


async def test_admin_probe_trigger_uses_the_no_spend_fake_and_returns_only_safe_summary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    snapshot, entry = await _seed_catalog(session_factory, content_hash="e" * 64)
    app = create_app(session_factory=session_factory, auth_required=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/admin/openrouter/probes/run",
            json={
                "catalog_content_hash": snapshot.content_hash,
                "canonical_slug": entry.canonical_slug,
            },
        )
        repeated = await client.post(
            "/admin/openrouter/probes/run",
            json={
                "catalog_content_hash": snapshot.content_hash,
                "canonical_slug": entry.canonical_slug,
            },
        )

    assert response.status_code == 200, response.text
    assert repeated.status_code == 200, repeated.text
    assert response.json()["created"] is True
    assert repeated.json()["created"] is False
    assert response.json()["status"] == "success"
    assert response.json()["cost_micros"] == 0
    rendered = response.text.lower()
    assert "authorization" not in rendered
    assert "api_key" not in rendered
    assert "raw_response" not in rendered


def test_catalog_probe_cli_runs_the_deterministic_no_spend_suite(tmp_path: Path) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'probe.sqlite'}"

    async def _seed() -> None:
        engine = create_engine(db_url)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            await _seed_catalog(create_session_factory(engine), content_hash="f" * 64)
        finally:
            await engine.dispose()

    asyncio.run(_seed())
    from padrino import cli

    result = CliRunner().invoke(
        cli.app,
        [
            "catalog",
            "probe",
            "--catalog-content-hash",
            "f" * 64,
            "--model",
            "openai/gpt-5-mini",
            "--db-url",
            db_url,
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["catalog_content_hash"] == "f" * 64
    assert summary["canonical_slug"] == "openai/gpt-5-mini"
    assert summary["created"] is True
    assert summary["cost_micros"] == 0
    assert "authorization" not in result.output.lower()
    assert "raw_response" not in result.output.lower()
