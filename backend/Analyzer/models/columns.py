"""Column helpers shared by the tables.

Small, but worth one home: both the account tables and the conversation tables
need the same id and timestamp shapes, and a second definition of "a
timezone-aware timestamp column" is a second chance to get it wrong.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlmodel import Column, DateTime


def uuid_hex() -> str:
    """A primary key: a UUID4 with the dashes taken out."""
    return uuid.uuid4().hex


def utcnow() -> datetime:
    """Timezone-aware now, so comparisons never mix naive and aware values."""
    return datetime.now(UTC)


def timestamp_column(**kwargs: object) -> Column:
    """A timezone-aware timestamp column.

    Spelled out as an explicit column because SQLModel would otherwise map a
    ``datetime`` to a naive ``DATETIME``, and a naive value read back cannot be
    compared against an aware one without raising.
    """
    return Column(DateTime(timezone=True), **kwargs)


def as_utc(value: datetime) -> datetime:
    """Read a stored timestamp back as an aware UTC one.

    SQLite hands back naive datetimes whatever the column says; they were
    written as UTC, so this is the reading that matches what was stored.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
