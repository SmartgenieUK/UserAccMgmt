from __future__ import annotations

import uuid
from sqlalchemy import DateTime, ForeignKey, Enum, func, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UUID_TYPE
from app.models.enums import Role


class Membership(Base):
    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    user_id: Mapped = mapped_column(UUID_TYPE, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    org_id: Mapped = mapped_column(UUID_TYPE, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[Role] = mapped_column(
        Enum(Role, values_callable=lambda e: [i.value for i in e], name="role"),
        nullable=False,
    )
    created_at: Mapped = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="memberships")
    organization = relationship("Organization", back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("user_id", "org_id", name="uq_membership_user_org"),
        Index("ix_memberships_org_id", "org_id"),
    )
