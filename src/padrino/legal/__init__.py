"""Versioned product-authored legal document registry."""

from __future__ import annotations

from padrino.legal.documents import (
    DOCUMENT_KIND_AGE_GATE,
    DOCUMENT_KIND_PRIVACY,
    DOCUMENT_KIND_RANKED,
    DOCUMENT_KIND_TOS,
    LegalDocument,
    get_legal_document,
    legal_document_payload,
    list_legal_documents,
)

__all__ = [
    "DOCUMENT_KIND_AGE_GATE",
    "DOCUMENT_KIND_PRIVACY",
    "DOCUMENT_KIND_RANKED",
    "DOCUMENT_KIND_TOS",
    "LegalDocument",
    "get_legal_document",
    "legal_document_payload",
    "list_legal_documents",
]
