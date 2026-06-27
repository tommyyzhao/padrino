"""Pure legal-document normalization and content hashing.

The normalization rule is intentionally small and stable:

* convert CRLF and CR line endings to LF;
* strip trailing spaces and tabs from each line;
* remove leading/trailing blank lines; and
* end the normalized body with exactly one LF.

This makes content hashes insensitive to editor line-ending churn and trailing
whitespace while preserving every meaningful character in the document body.
"""

from __future__ import annotations

import hashlib


def normalize_legal_document(text: str) -> str:
    """Return the canonical text used for legal-document content hashing."""
    unix_text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip(" \t") for line in unix_text.split("\n")]
    normalized = "\n".join(lines).strip("\n")
    return f"{normalized}\n"


def hash_legal_document(text: str) -> str:
    """Return sha256 over the normalized UTF-8 legal document body."""
    return hashlib.sha256(normalize_legal_document(text).encode("utf-8")).hexdigest()


__all__ = ["hash_legal_document", "normalize_legal_document"]
