from __future__ import annotations

import uuid
from sqlalchemy import String, DateTime, ForeignKey, Enum, func, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UUID_TYPE
from app.models.enums import Role


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    org_id: Mapped = mapped_column(UUID_TYPE, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    inviter_user_id: Mapped = mapped_column(UUID_TYPE, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[Role] = mapped_column(
        Enum(Role, values_callable=lambda e: [i.value for i in e], name="role"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    organization = relationship("Organization", back_populates="invitations")

    __table_args__ = (Index("ix_invitations_org_id", "org_id"),)
