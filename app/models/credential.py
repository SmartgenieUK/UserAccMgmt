from __future__ import annotations

from sqlalchemy import String, DateTime, Integer, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UUID_TYPE


class Credential(Base):
    __tablename__ = "credentials"

    user_id: Mapped = mapped_column(UUID_TYPE, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    password_changed_at: Mapped = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lockout_until: Mapped = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="credential")
