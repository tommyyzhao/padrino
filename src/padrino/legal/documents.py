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
DOCUMENT_KIND_RANKED = "RANKED"
DEFAULT_RANKED_CHAT_RETENTION_TTL_DAYS = 365
_BASE_VERSION_LABEL = "2026-07-12"
_RANKED_VERSION_LABEL = "2026-06-27"


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
Version: 2026-07-12

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

If you choose to connect an OpenRouter account for your own private-game
inference, Padrino receives a short-lived authorization code and stores the
resulting user-controlled API credential only as an encrypted server-side
envelope. The credential is never shown back to your browser and may be
disconnected or deleted from Padrino.
"""

_PRIVACY_BODY = """\
Padrino Privacy Policy
Version: 2026-07-12

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

When you voluntarily connect OpenRouter for private-game inference, Padrino
temporarily processes the authorization code and user-controlled API key to
validate it, then stores only an encrypted credential envelope, nonce, key
version, safe display label/suffix, and connection lifecycle timestamps. The
raw credential is not stored in browser storage, events, replays, exports,
analytics, or logs. Disconnect and account deletion erase the local envelope;
Padrino may also attempt upstream key revocation when OpenRouter supports it.

For promotional-credit play, Padrino retains an append-only integer-micro
ledger of grants, holds, charges, releases, and bounded operator completion
subsidies. Account deletion removes connection material and cancellable
pre-launch holds while preserving pseudonymized required charge and replay
audit records. You may request a self-export containing safe connection
metadata and wallet ledger facts; it never includes credential envelopes,
nonces, management handles, or authorization errors. Expired connection-flow
bindings and revoked legacy envelopes are removed on configured retention TTLs.
"""

_AGE_GATE_BODY = """\
Padrino Age Confirmation
Version: 2026-06-18

By playing Padrino, you confirm that you are 16+ years of age. If you
are under 16, do not create a guest session, sign in, join a lobby, or submit
chat or actions.
"""

_RANKED_BODY = f"""\
Padrino Ranked Human ELO Consent
Version: 2026-06-27

Ranked play is an optional human-vs-AI Mafia skill experiment. It is separate
from casual games and from the scientific benchmark. You must be 16+ and hold
current Terms, Privacy, and age confirmation consent before ranked play.

By opting into ranked play, you consent to the following ranked purposes:

Persistent rating: Padrino may write and retain a Human ELO rating, rating
events, game counts, and conservative score derived from ranked games.

Public handle: Padrino may display your pseudonymous ranked handle and ranked
card on public leaderboard surfaces. Raw display names and raw account IDs are
not used for public ranked display.

Chat retention: Padrino may retain ranked-game chat sidecar rows for up to
{DEFAULT_RANKED_CHAT_RETENTION_TTL_DAYS} days for game integrity, audit,
moderation, and future self-serve export/delete flows.

Anti-cheat profiling: Padrino may compute and retain advisory integrity signals
such as hashed IP clusters, cadence signals, and ranked-game telemetry for
manual review. v1 uses these signals for flagging only, not automatic penalties.

No cash prizes are offered. Human ratings and handles have no cash value and
are not redeemable for money or paid benefits. Ranked mode is provided as-is for
MVP testing with no warranty of availability, ranking value, or legal effect.
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
    LegalDocument(
        kind=DOCUMENT_KIND_RANKED,
        version_label=_RANKED_VERSION_LABEL,
        body=_RANKED_BODY,
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
    "DEFAULT_RANKED_CHAT_RETENTION_TTL_DAYS",
    "DOCUMENT_KIND_AGE_GATE",
    "DOCUMENT_KIND_PRIVACY",
    "DOCUMENT_KIND_RANKED",
    "DOCUMENT_KIND_TOS",
    "LegalDocument",
    "get_legal_document",
    "legal_document_payload",
    "list_legal_documents",
]
