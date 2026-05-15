"""Tests for :mod:`padrino.llm.secrets`.

The resolver supports two schemes — ``env:VAR`` and ``file:/abs/path`` — and
fails loudly with :class:`SecretResolutionError` on anything malformed so a
deployment can't silently 401 at request time when a credential is missing.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from padrino.llm.secrets import SecretResolutionError, resolve_secret


def test_env_scheme_returns_environment_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADRINO_TEST_SECRET", "sekret-token")
    assert resolve_secret("env:PADRINO_TEST_SECRET") == "sekret-token"


def test_env_scheme_strips_trailing_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADRINO_TEST_SECRET", "sekret-token\n  \t\r\n")
    assert resolve_secret("env:PADRINO_TEST_SECRET") == "sekret-token"


def test_env_scheme_missing_var_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADRINO_TEST_SECRET_MISSING", raising=False)
    with pytest.raises(SecretResolutionError) as exc_info:
        resolve_secret("env:PADRINO_TEST_SECRET_MISSING")
    assert "PADRINO_TEST_SECRET_MISSING" in str(exc_info.value)


def test_env_scheme_empty_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADRINO_TEST_SECRET_EMPTY", "   \n  ")
    with pytest.raises(SecretResolutionError) as exc_info:
        resolve_secret("env:PADRINO_TEST_SECRET_EMPTY")
    assert "empty" in str(exc_info.value).lower()


def test_file_scheme_returns_file_contents(tmp_path: Path) -> None:
    secret_file = tmp_path / "api.key"
    secret_file.write_text("file-sekret\n", encoding="utf-8")
    secret_file.chmod(0o600)
    assert resolve_secret(f"file:{secret_file}") == "file-sekret"


def test_file_scheme_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing.key"
    with pytest.raises(SecretResolutionError) as exc_info:
        resolve_secret(f"file:{missing}")
    assert str(missing) in str(exc_info.value)


def test_file_scheme_world_readable_raises(tmp_path: Path) -> None:
    secret_file = tmp_path / "api.key"
    secret_file.write_text("sekret", encoding="utf-8")
    secret_file.chmod(0o644)  # world-readable
    with pytest.raises(SecretResolutionError) as exc_info:
        resolve_secret(f"file:{secret_file}")
    msg = str(exc_info.value).lower()
    assert "permission" in msg or "world-readable" in msg


def test_file_scheme_empty_file_after_strip_raises(tmp_path: Path) -> None:
    secret_file = tmp_path / "api.key"
    secret_file.write_text("   \n\t\n", encoding="utf-8")
    secret_file.chmod(0o600)
    with pytest.raises(SecretResolutionError) as exc_info:
        resolve_secret(f"file:{secret_file}")
    assert "empty" in str(exc_info.value).lower()


def test_file_scheme_rejects_relative_path() -> None:
    with pytest.raises(SecretResolutionError) as exc_info:
        resolve_secret("file:relative/path/secret.key")
    assert "absolute" in str(exc_info.value).lower()


def test_file_scheme_rejects_tilde_path() -> None:
    with pytest.raises(SecretResolutionError) as exc_info:
        resolve_secret("file:~/secret.key")
    assert "absolute" in str(exc_info.value).lower()


def test_unknown_scheme_raises() -> None:
    with pytest.raises(SecretResolutionError) as exc_info:
        resolve_secret("vault:secret/data/api")
    msg = str(exc_info.value).lower()
    assert "scheme" in msg or "unknown" in msg


def test_missing_scheme_separator_raises() -> None:
    with pytest.raises(SecretResolutionError) as exc_info:
        resolve_secret("no-scheme-here")
    msg = str(exc_info.value).lower()
    assert "scheme" in msg or "unknown" in msg


def test_env_scheme_empty_var_name_raises() -> None:
    with pytest.raises(SecretResolutionError):
        resolve_secret("env:")


def test_file_scheme_directory_target_raises(tmp_path: Path) -> None:
    # Pointing at a directory should fail even if permissions look fine.
    tmp_path.chmod(0o700)
    with pytest.raises(SecretResolutionError):
        resolve_secret(f"file:{tmp_path}")


def test_file_scheme_group_readable_raises(tmp_path: Path) -> None:
    secret_file = tmp_path / "api.key"
    secret_file.write_text("sekret", encoding="utf-8")
    secret_file.chmod(0o640)  # group-readable but not world-readable
    with pytest.raises(SecretResolutionError):
        resolve_secret(f"file:{secret_file}")


def test_world_readable_check_is_posix_mode_bit(tmp_path: Path) -> None:
    """Sanity: the permission check uses the S_IROTH bit, not file owner."""
    secret_file = tmp_path / "api.key"
    secret_file.write_text("sekret", encoding="utf-8")
    secret_file.chmod(0o600)
    mode = secret_file.stat().st_mode
    assert not (mode & stat.S_IROTH)
    assert resolve_secret(f"file:{secret_file}") == "sekret"
