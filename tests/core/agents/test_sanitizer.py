"""Tests for the output sanitizer."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from padrino.core.agents.sanitizer import SanitizationResult, sanitize_visible_text


def test_passthrough_clean_text() -> None:
    result = sanitize_visible_text("hello world", 100)
    assert isinstance(result, SanitizationResult)
    assert result.cleaned == "hello world"
    assert result.truncated is False
    assert result.replacements == []


def test_nfkc_normalizes_compatibility_chars() -> None:
    # Fullwidth A, B, C -> ASCII A, B, C via NFKC.
    raw = "\uff21\uff22\uff23"
    result = sanitize_visible_text(raw, 100)
    assert result.cleaned == "ABC"


def test_nfkc_normalizes_nbsp_and_collapses() -> None:
    # NBSP -> space via NFKC, then collapsed with adjacent space.
    raw = "a\u00a0 b"
    result = sanitize_visible_text(raw, 100)
    assert result.cleaned == "a b"


def test_strip_zero_width_chars() -> None:
    raw = "he​llo‌‍world﻿"
    result = sanitize_visible_text(raw, 100)
    assert result.cleaned == "helloworld"
    assert "ZERO_WIDTH" in result.replacements


def test_strip_control_chars_preserves_newline_for_step_two() -> None:
    # \x00 \x01 stripped; \n preserved through step 2 then collapsed at step 3.
    result = sanitize_visible_text("a\x00b\x01c\nd", 100)
    assert result.cleaned == "abc d"
    assert "CONTROL_CHAR" in result.replacements


def test_collapse_whitespace_runs() -> None:
    result = sanitize_visible_text("a    b\t\tc   d", 100)
    assert result.cleaned == "a b c d"
    assert "WHITESPACE_COLLAPSED" in result.replacements


def test_collapse_does_not_flag_when_already_normal() -> None:
    result = sanitize_visible_text("a b c", 100)
    assert "WHITESPACE_COLLAPSED" not in result.replacements


def test_limit_repeated_punctuation_to_three() -> None:
    result = sanitize_visible_text("wow!!!!! really????", 100)
    assert result.cleaned == "wow!!! really???"
    assert "PUNCT_REPEAT" in result.replacements


def test_three_repeated_punct_unchanged() -> None:
    result = sanitize_visible_text("ok??? sure!!!", 100)
    assert result.cleaned == "ok??? sure!!!"
    assert "PUNCT_REPEAT" not in result.replacements


def test_strip_markdown_code_block() -> None:
    result = sanitize_visible_text("hi ```secret stuff``` bye", 100)
    assert "secret" not in result.cleaned
    assert "```" not in result.cleaned
    assert "CODE_BLOCK" in result.replacements


def test_strip_markdown_table() -> None:
    raw = "intro | a | b | c | outro"
    result = sanitize_visible_text(raw, 100)
    assert "|" not in result.cleaned
    assert "TABLE" in result.replacements


def test_single_pipe_not_treated_as_table() -> None:
    result = sanitize_visible_text("use the | symbol", 100)
    assert "|" in result.cleaned
    assert "TABLE" not in result.replacements


def test_replace_url_http() -> None:
    result = sanitize_visible_text("visit http://example.com/x for info", 100)
    assert result.cleaned == "visit [URL] for info"
    assert "URL" in result.replacements


def test_replace_url_https() -> None:
    result = sanitize_visible_text("see https://example.com/path?q=1 ok", 100)
    assert "[URL]" in result.cleaned
    assert "example.com" not in result.cleaned
    assert "URL" in result.replacements


def test_replace_long_base64_like() -> None:
    raw = "data aGVsbG8gd29ybGQgYmFzZTY0YWJjMTIzNDU= rest"
    result = sanitize_visible_text(raw, 200)
    assert "[ENCODED]" in result.cleaned
    assert "aGVsbG8" not in result.cleaned
    assert "ENCODED" in result.replacements


def test_replace_long_hex_like() -> None:
    raw = "hash 0123456789abcdef0123456789abcdef0123 done"
    result = sanitize_visible_text(raw, 200)
    assert "[ENCODED]" in result.cleaned
    assert "ENCODED" in result.replacements


def test_short_alphanumeric_not_encoded() -> None:
    result = sanitize_visible_text("abc123 xyz", 100)
    assert result.cleaned == "abc123 xyz"
    assert "ENCODED" not in result.replacements


def test_long_plain_letters_not_encoded() -> None:
    # 30 chars of pure non-hex letters; should NOT be flagged as base64/hex-like.
    raw = "supersupersupersupersupersupzz"
    result = sanitize_visible_text(raw, 200)
    assert result.cleaned == raw
    assert "ENCODED" not in result.replacements


def test_twentythree_chars_not_encoded() -> None:
    raw = "abc123abc123abc123abc12"  # 23 chars
    result = sanitize_visible_text(raw, 100)
    assert "ENCODED" not in result.replacements
    assert result.cleaned == raw


def test_truncate_overlong_suffix() -> None:
    result = sanitize_visible_text("z" * 50, 10)
    assert result.cleaned == "z" * 10
    assert result.truncated is True


def test_truncate_preserves_prefix() -> None:
    result = sanitize_visible_text("hello world", 5)
    assert result.cleaned == "hello"
    assert result.truncated is True


def test_no_truncation_when_within_limit() -> None:
    result = sanitize_visible_text("hello", 100)
    assert result.truncated is False
    assert result.cleaned == "hello"


def test_chained_transformations() -> None:
    raw = "look​ at https://x.com !!!!!  and ```code```"
    result = sanitize_visible_text(raw, 500)
    assert "​" not in result.cleaned
    assert "[URL]" in result.cleaned
    assert "x.com" not in result.cleaned
    assert "```" not in result.cleaned
    assert "code" not in result.cleaned
    assert "!!!!" not in result.cleaned
    assert result.truncated is False
    assert {"URL", "CODE_BLOCK", "PUNCT_REPEAT", "ZERO_WIDTH"}.issubset(set(result.replacements))


def test_truncation_after_replacement_expansion() -> None:
    # URL replacement happens before truncation; result is truncated to max_chars.
    raw = "x " + ("https://example.com/very-long-path " * 5)
    result = sanitize_visible_text(raw, 20)
    assert len(result.cleaned) == 20
    assert result.truncated is True


def test_empty_input() -> None:
    result = sanitize_visible_text("", 100)
    assert result.cleaned == ""
    assert result.truncated is False
    assert result.replacements == []


def test_max_chars_zero_truncates_to_empty() -> None:
    result = sanitize_visible_text("hello", 0)
    assert result.cleaned == ""
    assert result.truncated is True


def test_result_is_frozen() -> None:
    result = sanitize_visible_text("hello", 100)
    with pytest.raises(ValidationError):
        result.cleaned = "modified"  # type: ignore[misc]


def test_sanitizer_module_has_no_forbidden_imports() -> None:
    """Pure-core firewall."""

    src = Path("src/padrino/core/agents/sanitizer.py").read_text()
    tree = ast.parse(src)
    forbidden = {
        "padrino.db",
        "padrino.llm",
        "padrino.api",
        "padrino.runner",
        "sqlalchemy",
        "litellm",
        "httpx",
        "random",
        "secrets",
        "time",
        "datetime",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert alias.name not in forbidden, alias.name
                assert root not in forbidden, root
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            root = node.module.split(".")[0]
            assert node.module not in forbidden, node.module
            assert root not in forbidden, root
