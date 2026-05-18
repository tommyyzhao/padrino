"""Padrino: deterministic LLM benchmark and league engine for Mafia-style social deduction."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("padrino")
except PackageNotFoundError:  # pragma: no cover - only hit when not installed
    __version__ = "0.0.0+unknown"
