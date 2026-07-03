"""Tests for content-hash-bound legal document helpers (US-302)."""

from __future__ import annotations

import ast
from pathlib import Path

from padrino.core.legal_documents import hash_legal_document, normalize_legal_document


def test_hash_normalizes_line_endings_and_trailing_whitespace() -> None:
    text = "Line one  \r\nLine two\t\r\n"
    equivalent = "\nLine one\nLine two\n\n"

    assert normalize_legal_document(text) == "Line one\nLine two\n"
    assert hash_legal_document(text) == hash_legal_document(equivalent)


def test_hash_changes_when_body_changes() -> None:
    assert hash_legal_document("No cash prizes.\n") != hash_legal_document(
        "No cash prizes. Eligibility changed.\n"
    )


def test_legal_document_hash_module_stays_pure() -> None:
    path = Path("src/padrino/core/legal_documents.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")

    forbidden = {"random", "datetime", "time", "sqlalchemy", "padrino.db", "padrino.settings"}
    assert not {name for name in imports if name in forbidden or name.startswith("padrino.db.")}
