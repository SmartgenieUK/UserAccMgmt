from __future__ import annotations

import uuid
from sqlalchemy import String, DateTime, ForeignKey, Enum, func, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UUID_TYPE
from app.models.enums import VerificationTokenType


class VerificationToken(Base):
    __tablename__ = "verification_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    user_id: Mapped = mapped_column(UUID_TYPE, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    token_type: Mapped[VerificationTokenType] = mapped_column(Enum(VerificationTokenType), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_verification_tokens_type", "token_type"),)
