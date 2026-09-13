from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models import Membership, Plan, Subscription
from app.models.subscription import ENTITLING_STATUSES
from app.services.audit_service import AuditService
from app.utils.time import utcnow

FREE_TIER = "lite"


@dataclass
class Entitlement:
    org_id: str
    account_id: str  # the account anchor consumers key on; today the org id
    product: str
    tier: str
    scope: list[str]
    seats: int | None  # None = unlimited
    seats_used: int
    expires_at: datetime | None
    status: str  # subscription status, or "none" when the org has no subscription for the product
    source: str | None

    def as_dict(self) -> dict:
        return asdict(self)


class EntitlementService:
    """Derives an org's entitlement for a product from its subscription row. Fails to the free tier."""

    def __init__(self, session: AsyncSession, settings: Settings, audit_service: AuditService):
        self.session = session
        self.settings = settings
        self.audit_service = audit_service

    async def resolve(self, org_id: str, product: str) -> Entitlement:
        subscription = await self._subscription(org_id, product)
        plan: Plan | None = None
        if subscription and subscription.status in ENTITLING_STATUSES and (
            subscription.current_period_end is None or subscription.current_period_end > utcnow()
        ):
            plan = subscription.plan
        if plan is None:
            plan = await self._plan(product, FREE_TIER)
        seats = subscription.seats if plan is not None and subscription and plan is subscription.plan else plan.limits.get("seats")
        return Entitlement(
            org_id=str(org_id),
            account_id=str(org_id),
            product=product,
            tier=plan.code,
            scope=list(plan.features.get("scope", [])),
            seats=seats,
            seats_used=await self._seats_used(org_id, product),
            expires_at=subscription.current_period_end if subscription and plan is subscription.plan else None,
            status=subscription.status if subscription else "none",
            source=subscription.source if subscription else None,
        )

    async def override(
        self, org_id: str, product: str, tier: str, seats: int | None, expires_at: datetime | None, actor_user_id: str
    ) -> Entitlement:
        """Manual (contract) entitlement. The only writer of source=manual rows; billing owns source=stripe."""
        plan = await self._plan(product, tier)
        subscription = await self._subscription(org_id, product)
        if subscription is None:
            subscription = Subscription(org_id=org_id, product=product, plan_id=plan.id, status="active", source="manual")
            self.session.add(subscription)
        else:
            subscription.plan_id = plan.id
            subscription.status = "active"
            subscription.source = "manual"
        subscription.seats = seats
        subscription.current_period_end = expires_at
        await self.session.flush()
        await self.session.refresh(subscription)
        await self.audit_service.log_event(
            action="entitlement_overridden",
            user_id=actor_user_id,
            org_id=str(org_id),
            metadata={"product": product, "tier": tier, "seats": seats},
        )
        return await self.resolve(org_id, product)

    async def assign_seat(self, org_id: str, product: str, user_id: str, actor_user_id: str) -> Entitlement:
        membership = await self._membership(org_id, user_id)
        if product not in membership.products:
            entitlement = await self.resolve(org_id, product)
            if entitlement.seats is not None and entitlement.seats_used >= entitlement.seats:
                raise ConflictError("No seats available", code="seat_cap")
            membership.products = [*membership.products, product]
            await self.session.flush()
            await self.audit_service.log_event(
                action="seat_assigned", user_id=actor_user_id, org_id=str(org_id), metadata={"product": product, "seat_user_id": user_id}
            )
        return await self.resolve(org_id, product)

    async def unassign_seat(self, org_id: str, product: str, user_id: str, actor_user_id: str) -> Entitlement:
        membership = await self._membership(org_id, user_id)
        if product in membership.products:
            membership.products = [p for p in membership.products if p != product]
            await self.session.flush()
            await self.audit_service.log_event(
                action="seat_unassigned", user_id=actor_user_id, org_id=str(org_id), metadata={"product": product, "seat_user_id": user_id}
            )
        return await self.resolve(org_id, product)

    async def products(self) -> list[str]:
        result = await self.session.execute(select(Plan.product).where(Plan.is_active.is_(True)).distinct())
        return sorted(result.scalars().all())

    async def _subscription(self, org_id: str, product: str) -> Subscription | None:
        result = await self.session.execute(
            select(Subscription).where(Subscription.org_id == org_id, Subscription.product == product)
        )
        return result.scalar_one_or_none()

    async def _plan(self, product: str, code: str) -> Plan:
        result = await self.session.execute(
            select(Plan).where(Plan.product == product, Plan.code == code, Plan.is_active.is_(True))
        )
        plan = result.scalar_one_or_none()
        if plan is None:
            raise ValidationError(f"Unknown product or tier: {product}/{code}", code="unknown_plan")
        return plan

    async def _membership(self, org_id: str, user_id: str) -> Membership:
        result = await self.session.execute(
            select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
        )
        membership = result.scalar_one_or_none()
        if membership is None:
            raise NotFoundError("User is not a member of this organization", code="membership_missing")
        return membership

    async def _seats_used(self, org_id: str, product: str) -> int:
        result = await self.session.execute(select(Membership.products).where(Membership.org_id == org_id))
        return sum(1 for products in result.scalars().all() if product in (products or []))
