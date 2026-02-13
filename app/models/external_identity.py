from __future__ import annotations

import uuid
from sqlalchemy import String, DateTime, ForeignKey, Enum, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UUID_TYPE
from app.models.enums import ExternalProvider


class ExternalIdentity(Base):
    __tablename__ = "external_identities"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    user_id: Mapped = mapped_column(UUID_TYPE, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[ExternalProvider] = mapped_column(
        Enum(
            ExternalProvider,
            values_callable=lambda e: [i.value for i in e],
            name="external_provider",
        ),
        nullable=False,
    )
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", back_populates="external_identities")
