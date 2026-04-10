from __future__ import annotations

import uuid
from sqlalchemy import String, Boolean, DateTime, ForeignKey, func, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UUID_TYPE, JSONB_TYPE


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    org_id: Mapped = mapped_column(UUID_TYPE, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    redirect_uris: Mapped = mapped_column(JSONB_TYPE, nullable=False, server_default="[]")
    allowed_scopes: Mapped = mapped_column(JSONB_TYPE, nullable=False, server_default="[]")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    organization = relationship("Organization", back_populates="applications")

    __table_args__ = (
        Index("ix_applications_org_id", "org_id"),
    )
