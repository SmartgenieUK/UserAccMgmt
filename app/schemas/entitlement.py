from __future__ import annotations

from datetime import datetime
from typing import Literal

from app.schemas.common import APIModel


class EntitlementRead(APIModel):
    org_id: str
    account_id: str
    product: str
    tier: str
    scope: list[str]
    seats: int | None
    seats_used: int
    expires_at: datetime | None
    status: str
    source: str | None


class EntitlementOverride(APIModel):
    """Manual (contract) entitlement — source=manual. Stripe-sourced rows are written only by billing."""

    tier: Literal["lite", "pro", "enterprise"]
    seats: int | None = None
    expires_at: datetime | None = None


class SeatAssignRequest(APIModel):
    user_id: str
