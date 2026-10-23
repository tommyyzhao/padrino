"""Postgres rejects identifiers over 63 chars; SQLite hides this."""

from padrino.db import models  # noqa: F401  (registers tables on Base.metadata)
from padrino.db.base import Base

PG_MAX = 63


def test_metadata_identifiers_fit_postgres_limit() -> None:
    names: list[str] = []
    for table in Base.metadata.tables.values():
        names.append(table.name)
        names.extend(c.name for c in table.columns)
        names.extend(str(c.name) for c in table.indexes if c.name)
        names.extend(str(c.name) for c in table.constraints if c.name)
    too_long = sorted(n for n in names if len(n) > PG_MAX)
    assert not too_long, too_long
