from __future__ import annotations

import uuid
from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UUID_TYPE, TZ_DATETIME


class TesterRedemption(Base):
    """Durable one-use reservation; pending never grants entitlement.

    The UUID supplies the Stripe idempotency key. Retain applied rows after cancellation.
    Only server-resolved identifiers are stored, never the submitted code or payment data.
    """

    __tablename__ = "tester_redemptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    user_id: Mapped = mapped_column(UUID_TYPE, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    org_id: Mapped = mapped_column(UUID_TYPE, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    promotion_code_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tester_price_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stripe_customer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped = mapped_column(TZ_DATETIME, nullable=False)
    ends_at: Mapped = mapped_column(TZ_DATETIME, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_tester_redemption_user"),
        UniqueConstraint("org_id", name="uq_tester_redemption_org"),
        UniqueConstraint("stripe_subscription_id", name="uq_tester_redemption_subscription"),
    )
