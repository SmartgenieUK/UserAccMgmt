from __future__ import annotations

import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.models import Organization, Plan, Subscription, StripeEvent
from app.services.audit_service import AuditService
from app.services.stripe_gateway import StripeGateway


class BillingService:
    def __init__(self, session: AsyncSession, settings: Settings, audit_service: AuditService, gateway: StripeGateway):
        self.session = session
        self.settings = settings
        self.audit_service = audit_service
        self.gateway = gateway

    async def ensure_customer(self, org: Organization) -> str:
        if org.stripe_customer_id is None:
            metadata = {"org_id": str(org.id)}
            org.stripe_customer_id = self.gateway.create_customer(name=org.name, metadata=metadata)
            await self.session.flush()
        return org.stripe_customer_id

    async def create_checkout_session(self, org: Organization, price_id: str, actor_user_id: str) -> str:
        if price_id not in self.settings.STRIPE_PRICE_MAP:
            raise ValidationError("Unknown price id", code="unknown_price")
        
        customer_id = await self.ensure_customer(org)
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

    async def create_portal_session(self, org: Organization, actor_user_id: str) -> str:
        if not org.stripe_customer_id:
            raise ValidationError("Organization has no stripe customer", code="no_customer")
        
        url = self.gateway.create_portal_session(
            customer_id=org.stripe_customer_id,
            return_url=self.settings.STRIPE_PORTAL_RETURN_URL
        )
        
        await self.audit_service.log_event(
            action="billing_portal_opened",
            user_id=actor_user_id,
            org_id=str(org.id)
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
                "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None
            }
            for sub in subscriptions
        ]
        
        return {
            "stripe_customer_id": org.stripe_customer_id,
            "subscriptions": subs_list
        }

    async def handle_event(self, event: dict) -> None:
        event_id = event["id"]
        event_type = event["type"]
        
        # Idempotency
        existing_event = await self.session.get(StripeEvent, event_id)
        if existing_event:
            return
            
        stripe_event = StripeEvent(event_id=event_id, type=event_type)
        self.session.add(stripe_event)
        
        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            return
            
        data_object = event.get("data", {}).get("object", {})
        
        if event_type == "checkout.session.completed":
            metadata = data_object.get("metadata", {})
            price_id = metadata.get("price_id")
            
            if not price_id or price_id not in self.settings.STRIPE_PRICE_MAP:
                return
                
            product_tier = self.settings.STRIPE_PRICE_MAP[price_id]
            product, tier = product_tier.split(":", 1)
            
            org_id_str = metadata.get("org_id")
            if not org_id_str:
                return
                
            org_id = uuid.UUID(org_id_str)
            org = await self.session.get(Organization, org_id)
            if not org:
                return
                
            stripe_customer_id = data_object.get("customer")
            stripe_subscription_id = data_object.get("subscription")
            if not stripe_customer_id:
                return
                
            if org.stripe_customer_id != stripe_customer_id:
                org.stripe_customer_id = stripe_customer_id
                
            plan_result = await self.session.execute(
                select(Plan).where(Plan.product == product, Plan.code == tier)
            )
            plan = plan_result.scalar_one_or_none()
            if not plan:
                return
                
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

        elif event_type == "customer.subscription.updated":
            stripe_subscription_id = data_object.get("id")
            if not stripe_subscription_id:
                return
                
            sub_result = await self.session.execute(
                select(Subscription).where(Subscription.stripe_subscription_id == stripe_subscription_id)
            )
            subscription = sub_result.scalar_one_or_none()
            if not subscription:
                return
                
            stripe_status = data_object.get("status")
            status_map = {
                "active": "active",
                "trialing": "trialing",
                "past_due": "past_due",
                "canceled": "cancelled"
            }
            if stripe_status in status_map:
                subscription.status = status_map[stripe_status]
            
            # Note: current_period_end comes as a unix timestamp in stripe
            # wait, the spec says update current_period_end; we can do this if present in data_object.
            from datetime import datetime, timezone
            period_end = data_object.get("current_period_end")
            if period_end:
                subscription.current_period_end = datetime.fromtimestamp(period_end, tz=timezone.utc)
            
            # Check if price changed
            items = data_object.get("items", {}).get("data", [])
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
                metadata={"stripe_subscription_id": stripe_subscription_id, "status": subscription.status}
            )

        elif event_type == "customer.subscription.deleted":
            stripe_subscription_id = data_object.get("id")
            if not stripe_subscription_id:
                return
                
            sub_result = await self.session.execute(
                select(Subscription).where(Subscription.stripe_subscription_id == stripe_subscription_id)
            )
            subscription = sub_result.scalar_one_or_none()
            if not subscription:
                return
                
            subscription.status = "cancelled"
            await self.audit_service.log_event(
                action="billing_subscription_cancelled",
                org_id=str(subscription.org_id),
                metadata={"stripe_subscription_id": stripe_subscription_id}
            )
