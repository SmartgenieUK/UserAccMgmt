from sqlalchemy import String, JSON
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB as PG_JSONB

UUID_TYPE = PG_UUID(as_uuid=True).with_variant(String(36), "sqlite")
JSONB_TYPE = PG_JSONB().with_variant(JSON, "sqlite")
