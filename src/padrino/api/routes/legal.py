"""Public legal document routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict

from padrino.legal import get_legal_document, legal_document_payload, list_legal_documents

router = APIRouter(prefix="/legal", tags=["legal"])


class LegalDocumentResponse(BaseModel):
    """A served legal document with its content-hash binding."""

    model_config = ConfigDict(frozen=True)

    kind: str
    version_label: str
    content_hash: str
    body: str


class LegalDocumentsResponse(BaseModel):
    """Index response for legal documents."""

    model_config = ConfigDict(frozen=True)

    documents: list[LegalDocumentResponse]


@router.get("/documents", response_model=LegalDocumentsResponse)
async def get_legal_documents() -> LegalDocumentsResponse:
    """Return every versioned legal document body and content hash."""
    return LegalDocumentsResponse(
        documents=[
            LegalDocumentResponse(**legal_document_payload(document))
            for document in list_legal_documents()
        ]
    )


@router.get("/documents/{kind}", response_model=LegalDocumentResponse)
async def get_legal_document_by_kind(
    kind: str = Path(..., description="Legal document kind, such as TOS or PRIVACY"),
) -> LegalDocumentResponse:
    """Return one legal document by kind."""
    document = get_legal_document(kind)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="legal_document_not_found",
        )
    return LegalDocumentResponse(**legal_document_payload(document))


__all__ = ["router"]
