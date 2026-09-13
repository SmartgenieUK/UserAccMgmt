from __future__ import annotations

import uuid
from sqlalchemy import String, Integer, ForeignKey, func, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UUID_TYPE, TZ_DATETIME

SUBSCRIPTION_STATUSES = ("trialing", "active", "past_due", "cancelled")
ENTITLING_STATUSES = ("trialing", "active")  # anything else resolves to the free tier
SUBSCRIPTION_SOURCES = ("stripe", "manual")


class Subscription(Base):
    """One row per (org, product). Entitlement is derived from it, never stored per user."""

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    org_id: Mapped = mapped_column(UUID_TYPE, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    product: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_id: Mapped = mapped_column(UUID_TYPE, ForeignKey("plans.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    seats: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = unlimited
    current_period_end: Mapped = mapped_column(TZ_DATETIME, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped = mapped_column(TZ_DATETIME, server_default=func.now(), nullable=False)
    updated_at: Mapped = mapped_column(TZ_DATETIME, server_default=func.now(), onupdate=func.now(), nullable=False)

    plan = relationship("Plan", lazy="selectin")
    organization = relationship("Organization", back_populates="subscriptions")

    __table_args__ = (
        UniqueConstraint("org_id", "product", name="uq_subscription_org_product"),
        Index("ix_subscriptions_org_id", "org_id"),
    )
