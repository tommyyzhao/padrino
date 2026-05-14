"""Async SQLAlchemy persistence layer for Padrino.

Pure-core (``padrino.core``) must NEVER import from this package — see
``AGENTS.md`` hard rules. Routes, runners, and other impure callers go
through this module's async session factory.
"""
