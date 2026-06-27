"""Static legal documents served with content-hash binding.

The bodies are product-authored MVP text. They are intentionally stored in code
so the served body and the consent hash authority share one registry.
"""

from __future__ import annotations

from dataclasses import dataclass

from padrino.core.legal_documents import hash_legal_document

DOCUMENT_KIND_TOS = "TOS"
DOCUMENT_KIND_PRIVACY = "PRIVACY"
DOCUMENT_KIND_AGE_GATE = "AGE_GATE"
_BASE_VERSION_LABEL = "2026-06-18"


@dataclass(frozen=True, slots=True)
class LegalDocument:
    """A versioned legal document body."""

    kind: str
    version_label: str
    body: str

    @property
    def content_hash(self) -> str:
        """sha256 over the normalized body."""
        return hash_legal_document(self.body)


_TOS_BODY = """\
Padrino Terms & Conditions
Version: 2026-06-18

Padrino is an MVP social-deduction game and benchmark experience. By playing,
you agree to use the service casually, respectfully, and only for lawful
personal testing or entertainment.

You must be 16+ to play. If you are under 16, do not create a guest session,
sign in, join a lobby, or submit chat or actions.

The service is provided as-is for MVP testing with no warranty of availability,
accuracy, uninterrupted operation, ranking value, or fitness for a particular
purpose.

No cash prizes are offered. Ratings, handles, achievements, and game outcomes
have no cash value and are not redeemable for money or paid benefits.

Padrino may moderate or restrict access to protect the game, other players, and
the integrity of ranked human ELO experiments. Ranked mode, if enabled, is for
human-vs-AI Mafia skill measurement and remains separate from the scientific
benchmark.
"""

_PRIVACY_BODY = """\
Padrino Privacy Policy
Version: 2026-06-18

Padrino stores the minimum data needed to operate human games: guest or account
principal records, consent history, lobby membership, gameplay actions, chat
sidecar rows, rating rows when ranked human ELO is enabled, and operational
security metadata such as hashed IP-derived signals.

You must be 16+ to play. If you are under 16, do not create a guest session,
sign in, join a lobby, or submit chat or actions.

This MVP privacy notice is product-authored and provided as-is with no warranty
of completeness, availability, legal effect, or fitness for a particular
purpose. It will require formal legal review before prizes, paid features, or
marketing use.

No cash prizes are offered. Human ratings and public handles have no cash value
and are used only for game integrity, display, and research separation.

Human free-text chat is stored outside the deterministic game-event hash chain
so later redaction or erasure can remove personal text without changing replay
evidence. Raw IP addresses are not stored by the consent system; only hashes are
kept when an IP-derived signal is needed.
"""

_AGE_GATE_BODY = """\
Padrino Age Confirmation
Version: 2026-06-18

By playing Padrino, you confirm that you are 16+ years of age. If you
are under 16, do not create a guest session, sign in, join a lobby, or submit
chat or actions.
"""

_DOCUMENTS: tuple[LegalDocument, ...] = (
    LegalDocument(kind=DOCUMENT_KIND_TOS, version_label=_BASE_VERSION_LABEL, body=_TOS_BODY),
    LegalDocument(
        kind=DOCUMENT_KIND_PRIVACY,
        version_label=_BASE_VERSION_LABEL,
        body=_PRIVACY_BODY,
    ),
    LegalDocument(
        kind=DOCUMENT_KIND_AGE_GATE,
        version_label=_BASE_VERSION_LABEL,
        body=_AGE_GATE_BODY,
    ),
)

_DOCUMENTS_BY_KIND: dict[str, LegalDocument] = {doc.kind: doc for doc in _DOCUMENTS}


def list_legal_documents() -> tuple[LegalDocument, ...]:
    """Return every legal document in stable display order."""
    return _DOCUMENTS


def get_legal_document(kind: str) -> LegalDocument | None:
    """Return the document for *kind*, case-insensitively, or ``None``."""
    return _DOCUMENTS_BY_KIND.get(kind.strip().upper())


def legal_document_payload(document: LegalDocument) -> dict[str, str]:
    """Return the API payload for one document."""
    return {
        "kind": document.kind,
        "version_label": document.version_label,
        "content_hash": document.content_hash,
        "body": document.body,
    }


__all__ = [
    "DOCUMENT_KIND_AGE_GATE",
    "DOCUMENT_KIND_PRIVACY",
    "DOCUMENT_KIND_TOS",
    "LegalDocument",
    "get_legal_document",
    "legal_document_payload",
    "list_legal_documents",
]
