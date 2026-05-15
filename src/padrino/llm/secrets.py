"""Provider-credential resolution for ``auth_secret_ref`` strings.

A :class:`padrino.db.models.ModelProvider` row stores a reference string —
e.g. ``env:CEREBRAS_API_KEY`` or ``file:/run/secrets/anthropic`` — instead of
the raw credential. :func:`resolve_secret` turns that reference into the
underlying secret value at adapter-construction time so a misconfigured
deployment fails loudly at boot rather than silently 401-ing on the first
real provider call.

Two schemes are supported:

* ``env:VAR_NAME`` — read ``os.environ[VAR_NAME]``; the variable must be set
  and non-empty after stripping whitespace.
* ``file:/absolute/path`` — read the file, strip trailing whitespace; the
  path must be absolute (no ``~``, no relative segments) and the file must
  not be group- or world-readable on POSIX.

All other shapes (unknown scheme, missing separator, tilde or relative
paths, missing file, bad permissions, empty value) raise
:class:`SecretResolutionError`.

Impure module: lives in the ``llm`` layer and is never imported by pure-core.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

__all__ = [
    "SecretResolutionError",
    "resolve_secret",
]


class SecretResolutionError(RuntimeError):
    """Raised when an ``auth_secret_ref`` cannot be resolved to a credential."""


def resolve_secret(ref: str) -> str:
    """Resolve a provider ``auth_secret_ref`` string to its secret value.

    The returned value has trailing whitespace stripped and is guaranteed
    non-empty; any failure mode raises :class:`SecretResolutionError`.
    """
    if ":" not in ref:
        raise SecretResolutionError(
            f"auth_secret_ref {ref!r} has no scheme separator; "
            "expected 'env:VAR' or 'file:/abs/path'"
        )
    scheme, _, payload = ref.partition(":")
    if scheme == "env":
        return _resolve_env(payload)
    if scheme == "file":
        return _resolve_file(payload)
    raise SecretResolutionError(
        f"unknown auth_secret_ref scheme {scheme!r}; expected 'env' or 'file'"
    )


def _resolve_env(var_name: str) -> str:
    if not var_name:
        raise SecretResolutionError("env: scheme requires a variable name")
    try:
        raw = os.environ[var_name]
    except KeyError as exc:
        raise SecretResolutionError(f"environment variable {var_name!r} is not set") from exc
    value = raw.rstrip()
    if not value:
        raise SecretResolutionError(
            f"environment variable {var_name!r} is empty after stripping whitespace"
        )
    return value


def _resolve_file(raw_path: str) -> str:
    if not raw_path:
        raise SecretResolutionError("file: scheme requires a path")
    if raw_path.startswith("~"):
        raise SecretResolutionError(f"file path {raw_path!r} must be absolute (no '~' expansion)")
    path = Path(raw_path)
    if not path.is_absolute():
        raise SecretResolutionError(f"file path {raw_path!r} must be absolute")
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise SecretResolutionError(f"secret file {raw_path!r} does not exist") from exc
    except OSError as exc:
        raise SecretResolutionError(f"secret file {raw_path!r} is unreadable: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SecretResolutionError(f"secret path {raw_path!r} is not a regular file")
    if info.st_mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
        raise SecretResolutionError(
            f"secret file {raw_path!r} has insecure permissions "
            f"{stat.filemode(info.st_mode)}; restrict to owner-only (chmod 600)"
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SecretResolutionError(f"secret file {raw_path!r} could not be read: {exc}") from exc
    value = raw.rstrip()
    if not value:
        raise SecretResolutionError(f"secret file {raw_path!r} is empty after stripping whitespace")
    return value
