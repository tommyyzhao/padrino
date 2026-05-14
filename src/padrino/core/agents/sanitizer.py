"""Deterministic output sanitizer for agent-emitted visible chat.

The sanitizer applies a fixed, ordered pipeline of surface-level normalizations
so cosmetic encoding tricks (zero-width characters, base64 payloads, URLs,
markdown noise) cannot fingerprint or steganographically signal across messages.

Both the raw input and the cleaned output are surfaced to the caller — the raw
text is the input parameter, the cleaned text is on :class:`SanitizationResult`
— so the LLM call archive can store both for replay and audit.

Pure core: no DB / LLM / clock / network access.
"""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, ConfigDict


class SanitizationResult(BaseModel):
    """Outcome of a single sanitize pass."""

    model_config = ConfigDict(frozen=True)

    cleaned: str
    truncated: bool
    replacements: list[str]


_ZERO_WIDTH_CODEPOINTS: frozenset[int] = frozenset(
    {
        0x200B,  # zero-width space
        0x200C,  # zero-width non-joiner
        0x200D,  # zero-width joiner
        0x200E,  # left-to-right mark
        0x200F,  # right-to-left mark
        0x2028,  # line separator
        0x2029,  # paragraph separator
        0x2060,  # word joiner
        0x2061,  # function application
        0x2062,  # invisible times
        0x2063,  # invisible separator
        0x2064,  # invisible plus
        0xFEFF,  # zero-width no-break space / BOM
    }
)

_WHITESPACE_KEEP: frozenset[int] = frozenset({0x09, 0x0A, 0x0D})


def _is_zero_width(cp: int) -> bool:
    return cp in _ZERO_WIDTH_CODEPOINTS


def _is_stripped_control(cp: int) -> bool:
    if cp in _WHITESPACE_KEEP:
        return False
    return cp < 0x20 or 0x7F <= cp <= 0x9F


_WHITESPACE_RUN_RE = re.compile(r"\s+")
_PUNCT_REPEAT_RE = re.compile(r"([!?.,;:\-])\1{3,}")
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_TABLE_RE = re.compile(r"(?:\|[^|\n]*){2,}\|")
_URL_RE = re.compile(r"https?://\S+")
_ENCODED_CANDIDATE_RE = re.compile(r"[A-Za-z0-9+/=_-]{25,}")
_HEX_CHARS: frozenset[str] = frozenset("0123456789abcdefABCDEF")


def _record(replacements: list[str], tag: str) -> None:
    if tag not in replacements:
        replacements.append(tag)


def _strip_invisibles(text: str, replacements: list[str]) -> str:
    out: list[str] = []
    saw_zero_width = False
    saw_control = False
    for ch in text:
        cp = ord(ch)
        if _is_zero_width(cp):
            saw_zero_width = True
            continue
        if _is_stripped_control(cp):
            saw_control = True
            continue
        out.append(ch)
    if saw_zero_width:
        _record(replacements, "ZERO_WIDTH")
    if saw_control:
        _record(replacements, "CONTROL_CHAR")
    return "".join(out)


def _maybe_encoded(token: str) -> bool:
    has_digit = any(c.isdigit() for c in token)
    is_hex = all(c in _HEX_CHARS for c in token)
    has_base64_marker = any(c in "+/=" for c in token)
    return has_digit or is_hex or has_base64_marker


def _encoded_replacer(match: re.Match[str]) -> str:
    return "[ENCODED]" if _maybe_encoded(match.group(0)) else match.group(0)


def sanitize_visible_text(raw: str, max_chars: int) -> SanitizationResult:
    """Apply the ordered sanitization pipeline.

    Steps, in order:

    1. Unicode NFKC normalization.
    2. Strip zero-width characters and control characters (except newline,
       tab, and carriage return — those are folded by step 3).
    3. Collapse whitespace runs to a single space.
    4. Limit any repeated punctuation run to three identical characters.
    5. Strip markdown fenced code blocks and pipe-delimited tables.
    6. Replace URLs with ``[URL]``.
    7. Replace long base64/hex-like tokens (>24 chars) with ``[ENCODED]``.
    8. Enforce ``max_chars`` by truncating the suffix.
    """

    replacements: list[str] = []

    text = unicodedata.normalize("NFKC", raw)

    text = _strip_invisibles(text, replacements)

    collapsed = _WHITESPACE_RUN_RE.sub(" ", text)
    if collapsed != text:
        _record(replacements, "WHITESPACE_COLLAPSED")
    text = collapsed

    punct_clipped, count = _PUNCT_REPEAT_RE.subn(lambda m: m.group(1) * 3, text)
    if count:
        _record(replacements, "PUNCT_REPEAT")
    text = punct_clipped

    code_stripped, count = _CODE_BLOCK_RE.subn("", text)
    if count:
        _record(replacements, "CODE_BLOCK")
    text = code_stripped

    table_stripped, count = _TABLE_RE.subn("", text)
    if count:
        _record(replacements, "TABLE")
    text = table_stripped

    url_replaced, count = _URL_RE.subn("[URL]", text)
    if count:
        _record(replacements, "URL")
    text = url_replaced

    encoded_replaced = _ENCODED_CANDIDATE_RE.sub(_encoded_replacer, text)
    if encoded_replaced != text:
        _record(replacements, "ENCODED")
    text = encoded_replaced

    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    return SanitizationResult(cleaned=text, truncated=truncated, replacements=replacements)
