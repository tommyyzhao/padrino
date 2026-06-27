"""US-306: pure GDPR human-data export bundle shaping."""

from __future__ import annotations

import inspect
import json
import uuid
from datetime import UTC, datetime, timedelta

from padrino.core import gdpr_export
from padrino.core.gdpr_export import (
    ChatExportInput,
    ConsentExportInput,
    HumanRatingEventExportInput,
    HumanRatingExportInput,
    PrincipalExportInput,
    SessionExportInput,
    assemble_human_data_export,
)

_PRINCIPAL_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
_LEAGUE_ID = uuid.UUID("00000000-0000-4000-8000-000000000010")
_GAME_ID = uuid.UUID("00000000-0000-4000-8000-000000000020")
_NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def test_export_bundle_strips_sensitive_fields_and_forces_redacted_chat() -> None:
    bundle = assemble_human_data_export(
        principal=PrincipalExportInput(
            id=_PRINCIPAL_ID,
            kind="account",
            display_name="Ada",
            ranked_handle="RankedAda",
            ranked_handle_normalized="rankedada",
            deleted_at=None,
            created_at=_NOW - timedelta(days=10),
            updated_at=_NOW - timedelta(days=1),
        ),
        sessions=[
            SessionExportInput(
                id=uuid.UUID("00000000-0000-4000-8000-000000000101"),
                principal_id=_PRINCIPAL_ID,
                session_hash="session-secret-hash",
                kind="account",
                issued_at=_NOW - timedelta(days=2),
                expires_at=_NOW + timedelta(days=2),
                revoked_at=None,
                last_seen_at=_NOW,
            )
        ],
        consents=[
            ConsentExportInput(
                id=uuid.UUID("00000000-0000-4000-8000-000000000201"),
                subject_principal_id=_PRINCIPAL_ID,
                document_kind="RANKED:PUBLIC_HANDLE",
                document_version="ranked-v1",
                content_hash="content-hash",
                accepted_at=_NOW - timedelta(days=1),
                source_ip_hash="ip-hash-must-not-export",
            )
        ],
        chat_messages=[
            ChatExportInput(
                id=uuid.UUID("00000000-0000-4000-8000-000000000301"),
                game_id=_GAME_ID,
                sequence=7,
                public_player_id="P03",
                raw_text="this raw text was already erased",
                cleaned_text="this cleaned text was already erased",
                redacted=True,
                created_at=_NOW,
            )
        ],
        human_ratings=[
            HumanRatingExportInput(
                id=uuid.UUID("00000000-0000-4000-8000-000000000401"),
                league_id=_LEAGUE_ID,
                human_player_id=str(_PRINCIPAL_ID),
                scope_type="GLOBAL",
                scope_value="GLOBAL",
                mu=30.0,
                sigma=7.0,
                conservative_score=9.0,
                games=4,
                last_game_at=_NOW - timedelta(hours=2),
                quarantined_at=None,
                updated_at=_NOW - timedelta(hours=1),
            )
        ],
        human_rating_events=[
            HumanRatingEventExportInput(
                id=uuid.UUID("00000000-0000-4000-8000-000000000501"),
                league_id=_LEAGUE_ID,
                game_id=_GAME_ID,
                human_player_id=str(_PRINCIPAL_ID),
                public_player_id="P03",
                scope_type="GLOBAL",
                scope_value="GLOBAL",
                before_mu=25.0,
                before_sigma=8.333333333333334,
                after_mu=30.0,
                after_sigma=7.0,
                created_at=_NOW,
            )
        ],
    )

    assert bundle.schema_version == "padrino.gdpr_human_export.v1"
    assert bundle.chat_messages[0].redacted is True
    assert bundle.chat_messages[0].raw_text is None
    assert bundle.chat_messages[0].cleaned_text is None

    rendered = json.dumps(bundle.model_dump(mode="json"), sort_keys=True)
    assert "session_hash" not in rendered
    assert "session-secret-hash" not in rendered
    assert "source_ip_hash" not in rendered
    assert "ip-hash-must-not-export" not in rendered
    assert "this raw text was already erased" not in rendered
    assert "this cleaned text was already erased" not in rendered


def test_export_bundle_sorts_inputs_deterministically() -> None:
    newer_consent = ConsentExportInput(
        id=uuid.UUID("00000000-0000-4000-8000-000000000202"),
        subject_principal_id=_PRINCIPAL_ID,
        document_kind="TOS",
        document_version="tos-v1",
        content_hash="tos-hash",
        accepted_at=_NOW,
        source_ip_hash=None,
    )
    older_consent = ConsentExportInput(
        id=uuid.UUID("00000000-0000-4000-8000-000000000201"),
        subject_principal_id=_PRINCIPAL_ID,
        document_kind="PRIVACY",
        document_version="privacy-v1",
        content_hash="privacy-hash",
        accepted_at=_NOW - timedelta(days=1),
        source_ip_hash=None,
    )
    later_chat = ChatExportInput(
        id=uuid.UUID("00000000-0000-4000-8000-000000000302"),
        game_id=_GAME_ID,
        sequence=9,
        public_player_id="P03",
        raw_text="later",
        cleaned_text="later",
        redacted=False,
        created_at=_NOW,
    )
    earlier_chat = ChatExportInput(
        id=uuid.UUID("00000000-0000-4000-8000-000000000301"),
        game_id=_GAME_ID,
        sequence=3,
        public_player_id="P03",
        raw_text="earlier",
        cleaned_text="earlier",
        redacted=False,
        created_at=_NOW,
    )

    bundle_a = assemble_human_data_export(
        principal=PrincipalExportInput(
            id=_PRINCIPAL_ID,
            kind="guest",
            display_name=None,
            ranked_handle=None,
            ranked_handle_normalized=None,
            deleted_at=None,
            created_at=_NOW,
            updated_at=_NOW,
        ),
        sessions=[],
        consents=[newer_consent, older_consent],
        chat_messages=[later_chat, earlier_chat],
        human_ratings=[],
        human_rating_events=[],
    )
    bundle_b = assemble_human_data_export(
        principal=bundle_a.principal,
        sessions=[],
        consents=[older_consent, newer_consent],
        chat_messages=[earlier_chat, later_chat],
        human_ratings=[],
        human_rating_events=[],
    )

    assert [row.document_kind for row in bundle_a.consents] == ["PRIVACY", "TOS"]
    assert [row.sequence for row in bundle_a.chat_messages] == [3, 9]
    assert bundle_a.model_dump(mode="json") == bundle_b.model_dump(mode="json")


def test_export_bundle_is_pure_core_no_db_or_sqlalchemy_imports() -> None:
    source = inspect.getsource(gdpr_export)

    assert "sqlalchemy" not in source
    assert "padrino.db" not in source
    assert "datetime.now" not in source
    assert "utcnow" not in source
    assert "random" not in source
