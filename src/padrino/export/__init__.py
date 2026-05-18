"""Signed game-export bundles (US-061).

The :mod:`padrino.export.bundle` module exposes :func:`export_game` plus the
:class:`Ed25519Signer` helper used by the ``padrino export game`` CLI to emit
a tamper-evident JSON artifact containing one completed game's full event log
and metadata. See :mod:`padrino.export.bundle` for details.
"""

from padrino.export.bundle import (
    AgentBuildInfo,
    BundlePayloadUnsafeError,
    Ed25519Signer,
    EventEnvelope,
    ExportError,
    GameBundle,
    GameNotExportable,
    GameSeatInfo,
    assert_bundle_payload_safe,
    canonical_bundle_bytes,
    export_game,
    verify_bundle_signature,
)

__all__ = [
    "AgentBuildInfo",
    "BundlePayloadUnsafeError",
    "Ed25519Signer",
    "EventEnvelope",
    "ExportError",
    "GameBundle",
    "GameNotExportable",
    "GameSeatInfo",
    "assert_bundle_payload_safe",
    "canonical_bundle_bytes",
    "export_game",
    "verify_bundle_signature",
]
