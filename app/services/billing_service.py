from __future__ import annotations

import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.exceptions import ActivationError, ValidationError
from app.models import Organization, Plan, Subscription, StripeEvent
from app.services.audit_service import AuditService
from app.services.stripe_gateway import StripeGateway


class BillingService:
    def __init__(self, session: AsyncSession, settings: Settings, audit_service: AuditService, gateway: StripeGateway):
        self.session = session
        self.settings = settings
        self.audit_service = audit_service
        self.gateway = gateway

    async def ensure_customer(self, org: Organization, email: str) -> str:
        if org.stripe_customer_id is None:
            metadata = {"org_id": str(org.id)}
            org.stripe_customer_id = self.gateway.create_customer(name=org.name, email=email, metadata=metadata)
            await self.session.flush()
        return org.stripe_customer_id

    async def create_checkout_session(self, org: Organization, price_id: str, actor_user_id: str, actor_email: str) -> str:
        if price_id not in self.settings.STRIPE_PRICE_MAP:
            raise ValidationError("Unknown price id", code="unknown_price")
        
        customer_id = await self.ensure_customer(org, actor_email)
        metadata = {"org_id": str(org.id), "price_id": price_id}
        
        url = self.gateway.create_checkout_session(
            customer_id=customer_id,
            price_id=price_id,
            success_url=self.settings.STRIPE_CHECKOUT_SUCCESS_URL,
            cancel_url=self.settings.STRIPE_CHECKOUT_CANCEL_URL,
            metadata=metadata
        )
        
        await self.audit_service.log_event(
            action="billing_checkout_started",
            user_id=actor_user_id,
            org_id=str(org.id),
            metadata={"price_id": price_id}
        )
        return url

    async def create_portal_session(self, org: Organization, actor_user_id: str, flow: str | None = None) -> str:
        if not org.stripe_customer_id:
            raise ValidationError("Organization has no stripe customer", code="no_customer")

        cancel_subscription_id = None
        if flow == "cancel":
            result = await self.session.execute(
                select(Subscription.stripe_subscription_id).where(
                    Subscription.org_id == org.id, Subscription.stripe_subscription_id.is_not(None)
                )
            )
            cancel_subscription_id = result.scalars().first()
            if not cancel_subscription_id:
                raise ValidationError("Organization has no subscription to cancel", code="no_subscription")

        url = self.gateway.create_portal_session(
            customer_id=org.stripe_customer_id,
            return_url=self.settings.STRIPE_PORTAL_RETURN_URL,
            cancel_subscription_id=cancel_subscription_id,
        )

        await self.audit_service.log_event(
            action="billing_portal_opened",
            user_id=actor_user_id,
            org_id=str(org.id),
            metadata={"flow": flow} if flow else None,
        )
        return url

    async def billing_summary(self, org: Organization) -> dict:
        result = await self.session.execute(
            select(Subscription).where(Subscription.org_id == org.id)
        )
        subscriptions = result.scalars().all()
        
        subs_list = [
            {
                "product": sub.product,
                "tier": (await self.session.get(Plan, sub.plan_id)).code,
                "status": sub.status,
                "source": sub.source,
                "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
                "cancel_at_period_end": sub.cancel_at_period_end,
            }
            for sub in subscriptions
        ]
        
        return {
            "stripe_customer_id": org.stripe_customer_id,
            "subscriptions": subs_list
        }

    async def handle_event(self, event: dict) -> None:
        """Apply a Stripe event, then mark it processed in the same transaction.

        The StripeEvent row records success, not receipt. When the effect cannot be applied this raises
        ActivationError and records nothing; the route rolls back and answers 500, so Stripe's retry of the
        same event is processed afresh. An event type we do not handle is a recorded no-op.
        """
        event_id = event["id"]
        event_type = event["type"]

        # Idempotency: an event on record was applied; a duplicate delivery is a harmless no-op.
        if await self.session.get(StripeEvent, event_id):
            return

        data_object = event.get("data", {}).get("object", {})

        if event_type == "checkout.session.completed":
            await self._activate(data_object)
        elif event_type == "customer.subscription.updated":
            await self._update(data_object)
        elif event_type == "customer.subscription.deleted":
            await self._cancel(data_object)

        self.session.add(StripeEvent(event_id=event_id, type=event_type))
        try:
            await self.session.flush()
        except IntegrityError:
            # A concurrent delivery of the same event applied it first; drop ours.
            await self.session.rollback()

    async def _activate(self, data_object: dict) -> None:
        metadata = data_object.get("metadata", {})
        price_id = metadata.get("price_id")
        if not price_id or price_id not in self.settings.STRIPE_PRICE_MAP:
            raise ActivationError("unknown_price", org_id=metadata.get("org_id"))
        product, tier = self.settings.STRIPE_PRICE_MAP[price_id].split(":", 1)

        org_id_str = metadata.get("org_id")
        if not org_id_str:
            raise ActivationError("missing_org")
        try:
            org = await self.session.get(Organization, uuid.UUID(org_id_str))
        except ValueError:
            org = None
        if not org:
            raise ActivationError("unknown_org", org_id=org_id_str)

        # Paid, or a 100%-off promotion code; a completed-but-unpaid session is not a silent no-op.
        if data_object.get("payment_status") not in ("paid", "no_payment_required"):
            raise ActivationError("unpaid_session", org_id=org_id_str)

        stripe_customer_id = data_object.get("customer")
        if not stripe_customer_id:
            raise ActivationError("missing_customer", org_id=org_id_str)
        stripe_subscription_id = data_object.get("subscription")
        if not stripe_subscription_id:
            raise ActivationError("missing_subscription", org_id=org_id_str)
        if org.stripe_customer_id and org.stripe_customer_id != stripe_customer_id:
            raise ActivationError("customer_mismatch", org_id=org_id_str)
        org.stripe_customer_id = stripe_customer_id

        plan_result = await self.session.execute(
            select(Plan).where(Plan.product == product, Plan.code == tier)
        )
        plan = plan_result.scalar_one_or_none()
        if not plan:
            raise ActivationError("unknown_plan", org_id=org_id_str)

        sub_result = await self.session.execute(
            select(Subscription).where(Subscription.org_id == org.id, Subscription.product == product)
        )
        subscription = sub_result.scalar_one_or_none()

        if not subscription:
            subscription = Subscription(
                org_id=org.id,
                product=product,
                plan_id=plan.id,
                status="active",
                source="stripe",
                stripe_customer_id=stripe_customer_id,
                stripe_subscription_id=stripe_subscription_id
            )
            self.session.add(subscription)
        else:
            subscription.plan_id = plan.id
            subscription.status = "active"
            subscription.source = "stripe"
            subscription.stripe_customer_id = stripe_customer_id
            subscription.stripe_subscription_id = stripe_subscription_id

        await self.session.flush()
        await self.audit_service.log_event(
            action="billing_subscription_activated",
            org_id=str(org.id),
            metadata={"product": product, "tier": tier, "stripe_subscription_id": stripe_subscription_id}
        )

    async def _known_subscription(self, data_object: dict) -> Subscription:
        # A subscription event can arrive before its checkout event; 500 makes Stripe retry it later.
        stripe_subscription_id = data_object.get("id")
        subscription = None
        if stripe_subscription_id:
            sub_result = await self.session.execute(
                select(Subscription).where(Subscription.stripe_subscription_id == stripe_subscription_id)
            )
            subscription = sub_result.scalar_one_or_none()
        if not subscription:
            raise ActivationError("unknown_subscription")
        return subscription

    async def _update(self, data_object: dict) -> None:
        subscription = await self._known_subscription(data_object)

        stripe_status = data_object.get("status")
        status_map = {
            "active": "active",
            "trialing": "trialing",
            "past_due": "past_due",
            "canceled": "cancelled"
        }
        if stripe_status in status_map:
            subscription.status = status_map[stripe_status]

        # Since Stripe's 2025 API versions the period end lives on the subscription items; older versions put it on
        # the subscription. Every renewal sends this event, so a stored value is always refreshed (BRIEF-029).
        from datetime import datetime, timezone
        items = data_object.get("items", {}).get("data", [])
        period_end = (items[0].get("current_period_end") if items else None) or data_object.get("current_period_end")
        if period_end:
            subscription.current_period_end = datetime.fromtimestamp(period_end, tz=timezone.utc)
        subscription.cancel_at_period_end = bool(data_object.get("cancel_at_period_end"))

        # Check if price changed
        if items:
            price_id = items[0].get("price", {}).get("id")
            if price_id and price_id in self.settings.STRIPE_PRICE_MAP:
                product_tier = self.settings.STRIPE_PRICE_MAP[price_id]
                product, tier = product_tier.split(":", 1)
                plan_result = await self.session.execute(
                    select(Plan).where(Plan.product == product, Plan.code == tier)
                )
                plan = plan_result.scalar_one_or_none()
                if plan:
                    subscription.plan_id = plan.id

        await self.audit_service.log_event(
            action="billing_subscription_updated",
            org_id=str(subscription.org_id),
            metadata={"stripe_subscription_id": data_object["id"], "status": subscription.status}
        )

    async def _cancel(self, data_object: dict) -> None:
        subscription = await self._known_subscription(data_object)
        subscription.status = "cancelled"
        await self.audit_service.log_event(
            action="billing_subscription_cancelled",
            org_id=str(subscription.org_id),
            metadata={"stripe_subscription_id": data_object["id"]}
        )
