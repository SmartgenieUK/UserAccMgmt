from __future__ import annotations

import uuid
from sqlalchemy import String, Boolean, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UUID_TYPE, TZ_DATETIME


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped = mapped_column(TZ_DATETIME, server_default=func.now(), nullable=False)

    memberships = relationship("Membership", back_populates="organization", cascade="all, delete-orphan")
    invitations = relationship("Invitation", back_populates="organization", cascade="all, delete-orphan")
    applications = relationship("Application", back_populates="organization", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="organization", cascade="all, delete-orphan")
