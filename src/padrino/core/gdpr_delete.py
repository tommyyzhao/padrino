"""Pure helpers for GDPR delete anonymization.

The impure API/database layer owns clocks and row updates. This module only
derives stable UUID-shaped tombstone identifiers from a principal id so rating
history can remain auditably grouped without retaining the live principal id.
"""

from __future__ import annotations

import uuid
from typing import Final

_ANONYMIZED_PRINCIPAL_NAMESPACE: Final[uuid.UUID] = uuid.UUID(
    "8b9d3797-00bd-4a9f-8f50-452c3c25fa46"
)


def anonymized_principal_token(
    principal_id: uuid.UUID,
    *,
    collision_index: int = 0,
) -> str:
    """Return a stable UUID-shaped anonymized token for ``principal_id``.

    ``collision_index`` lets the database shell pick the next deterministic
    token if the first one collides with an existing live principal id.
    """
    if collision_index < 0:
        raise ValueError("collision_index must be non-negative")
    raw = f"padrino-human-gdpr-delete:{principal_id}:{collision_index}"
    return str(uuid.uuid5(_ANONYMIZED_PRINCIPAL_NAMESPACE, raw))


__all__ = ["anonymized_principal_token"]
