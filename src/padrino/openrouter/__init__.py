"""OpenRouter management-plane integrations.

This package is deliberately outside ``padrino.core``: catalog discovery is
network and database I/O, while the deterministic engine consumes only frozen
runtime snapshots derived from the persisted catalog.
"""

from __future__ import annotations
