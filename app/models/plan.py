from __future__ import annotations

import uuid
from sqlalchemy import String, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UUID_TYPE, JSONB_TYPE

PLAN_CODES = ("lite", "pro", "enterprise")
PRODUCTS = ("devgenie", "cloudgenie")

# Seeded by the plans_subscriptions migration and by the test fixtures — one source for both.
# features: {"scope": [...]} is what the entitlement resolver surfaces; limits: {"seats": n} caps seats.
DEFAULT_PLANS: list[dict] = [
    {"product": product, "code": code, "features": {}, "limits": {}, "is_active": True}
    for product in PRODUCTS
    for code in PLAN_CODES
]


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    product: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    features: Mapped[dict] = mapped_column(JSONB_TYPE, default=dict, nullable=False)
    limits: Mapped[dict] = mapped_column(JSONB_TYPE, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (UniqueConstraint("product", "code", name="uq_plan_product_code"),)
