from __future__ import annotations

import uuid
from datetime import timezone

from sqlalchemy import JSON, DateTime, TypeDecorator, Uuid
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB


class UUIDType(TypeDecorator):
    """Native UUID on PostgreSQL, CHAR(32) on SQLite; accepts uuid.UUID or its string form on bind.

    The services pass ids as strings in places (audit events, session.get); asyncpg coerced those
    silently, SQLite does not, so the coercion lives here and the tests run on the same types as prod.
    """

    impl = Uuid(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if isinstance(value, str):
            return uuid.UUID(value)
        return value


class TZDateTime(TypeDecorator):
    """timestamptz on PostgreSQL; on SQLite (no tz storage) re-attach UTC on read so comparisons
    against aware datetimes behave the same on both backends."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


UUID_TYPE = UUIDType()
TZ_DATETIME = TZDateTime()
JSONB_TYPE = PG_JSONB().with_variant(JSON, "sqlite")
