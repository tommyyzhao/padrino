"""Tests for served legal document registry routes (US-302)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from padrino.api.app import create_app
from padrino.core.legal_documents import hash_legal_document


@pytest.mark.asyncio
async def test_legal_documents_index_serves_hash_bound_tos_and_privacy() -> None:
    app = create_app(auth_required=True, public_surface_only=True)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get("/legal/documents")

    assert resp.status_code == 200
    documents = {doc["kind"]: doc for doc in resp.json()["documents"]}
    assert set(documents) == {"TOS", "PRIVACY", "AGE_GATE", "RANKED"}
    for doc in documents.values():
        assert doc["version_label"]
        assert doc["body"].strip()
        assert doc["content_hash"] == hash_legal_document(doc["body"])
        body = doc["body"].lower()
        assert "16+" in body
    for kind in {"TOS", "PRIVACY"}:
        body = documents[kind]["body"].lower()
        assert "no warranty" in body
        assert "no cash prizes" in body
        assert "openrouter" in body
        assert "encrypted" in body
    assert documents["RANKED"]["version_label"] != documents["TOS"]["version_label"]
    assert "persistent rating" in documents["RANKED"]["body"].lower()


@pytest.mark.asyncio
async def test_legal_document_detail_is_content_hash_bound() -> None:
    app = create_app(auth_required=True, public_surface_only=True)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get("/legal/documents/privacy")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["kind"] == "PRIVACY"
    assert payload["content_hash"] == hash_legal_document(payload["body"])


@pytest.mark.asyncio
async def test_unknown_legal_document_kind_returns_404() -> None:
    app = create_app(auth_required=True, public_surface_only=True)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get("/legal/documents/rules")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "legal_document_not_found"
