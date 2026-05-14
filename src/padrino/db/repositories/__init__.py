"""Async repository modules for Padrino's persistence layer.

Each module exposes ``create`` / ``get`` / ``list_`` async functions over a
single ORM model. Higher layers (API routes, runner) call into these helpers
so they never touch SQLAlchemy directly.
"""

from __future__ import annotations
