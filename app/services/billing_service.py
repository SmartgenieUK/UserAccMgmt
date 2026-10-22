from __future__ import annotations

import uuid
from datetime import datetime, timezone

import stripe
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.exceptions import ActivationError, AppError, ConflictError, ValidationError
from app.models import Organization, Plan, Subscription, StripeEvent
from app.services.audit_service import AuditService
from app.services.stripe_gateway import StripeGateway

# BUG-08: Stripe statuses that still hold (or may yet hold) a paid subscription, and how each lands in UAM.
LIVE_STRIPE_STATUSES = ("trialing", "active", "past_due", "unpaid", "incomplete", "paused")
UAM_STATUS = {
    "active": "active",
    "trialing": "trialing",
    "past_due": "past_due",
    "unpaid": "past_due",
    "incomplete": "past_due",
    "paused": "past_due",
    "canceled": "cancelled",
    "incomplete_expired": "cancelled",
}
LOCAL_LIVE_STATUSES = ("trialing", "active", "past_due")


def _at(obj, key):
    """Mapping access that tolerates a missing key or a missing object (Stripe's nested values are not dicts)."""
    try:
        return obj[key]
    except (KeyError, TypeError, IndexError):
        return None


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

    def _product_of(self, price_id) -> str | None:
        target = self.settings.STRIPE_PRICE_MAP.get(price_id) if price_id else None
        return target.split(":", 1)[0] if target else None

    def _product_item(self, stripe_sub, product: str):
        """The subscription item whose price maps to this product, and its tier; (None, None) when none does."""
        for item in _at(_at(stripe_sub, "items"), "data") or []:
            price_id = _at(_at(item, "price"), "id")
            if self._product_of(price_id) == product:
                return item, self.settings.STRIPE_PRICE_MAP[price_id].split(":", 1)[1]
        return None, None

    async def _lock(self, org_id, product: str):
        """Lock the organisation, then its (org, product) subscription row; refresh both. Held to commit/rollback."""
        org = (await self.session.execute(
            select(Organization).where(Organization.id == org_id)
            .with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        row = (await self.session.execute(
            select(Subscription).where(Subscription.org_id == org_id, Subscription.product == product)
            .with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        return org, row

    async def create_checkout_session(self, org: Organization, price_id: str, actor_user_id: str, actor_email: str) -> str:
        if price_id not in self.settings.STRIPE_PRICE_MAP:
            raise ValidationError("Unknown price id", code="unknown_price")
        product = self._product_of(price_id)

        # BUG-08: one live subscription per org and product. Lock, refuse a live one, reuse an open session.
        org, local = await self._lock(org.id, product)
        if local and local.status in LOCAL_LIVE_STATUSES and local.stripe_subscription_id:
            raise ConflictError("A live subscription exists", code="subscription_exists")
        if org.stripe_customer_id:
            try:
                # Sessions first: one that completes between the two calls shows up as a subscription.
                sessions = self.gateway.list_open_checkout_sessions(org.stripe_customer_id)
                subscriptions = self.gateway.list_subscriptions(org.stripe_customer_id)
            except stripe.error.StripeError:
                raise AppError("Stripe is unavailable", status_code=502, code="stripe_unavailable")
            for sub in subscriptions:
                if _at(sub, "status") in LIVE_STRIPE_STATUSES and self._product_item(sub, product)[0] is not None:
                    raise ConflictError("A live subscription exists", code="subscription_exists")
            for open_session in sessions:
                if self._product_of(_at(_at(open_session, "metadata"), "price_id")) == product:
                    await self.audit_service.log_event(
                        action="billing_checkout_reused",
                        user_id=actor_user_id,
                        org_id=str(org.id),
                        metadata={"price_id": price_id},
                    )
                    return open_session["url"]

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

    async def create_portal_session(
        self, org: Organization, actor_user_id: str, flow: str | None = None, product: str | None = None
    ) -> str:
        if not org.stripe_customer_id:
            raise ValidationError("Organization has no stripe customer", code="no_customer")

        cancel_subscription_id = None
        if flow == "cancel":
            result = await self.session.execute(
                # UAM is shared across products: cancel only this product's live Stripe subscription.
                select(Subscription.stripe_subscription_id).where(
                    Subscription.org_id == org.id,
                    Subscription.product == product,
                    Subscription.status.in_(("active", "trialing", "past_due")),
                    Subscription.stripe_subscription_id.is_not(None),
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
            metadata={"flow": flow, "product": product} if flow else None,
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

    def _retrieve(self, stripe_subscription_id: str, org_id) -> dict:
        try:
            return self.gateway.retrieve_subscription(stripe_subscription_id)
        except stripe.error.StripeError:
            raise ActivationError("stripe_unavailable", org_id=str(org_id))

    async def _reconcile(self, row: Subscription, stripe_sub: dict, replacement: bool) -> None:
        """BUG-08: the row takes Stripe's current state for its own subscription, never an event body's snapshot."""
        org_id = str(row.org_id)
        if _at(stripe_sub, "id") != row.stripe_subscription_id or _at(stripe_sub, "customer") != row.stripe_customer_id:
            raise ActivationError("stripe_mismatch", org_id=org_id)
        item, tier = self._product_item(stripe_sub, row.product)
        plan = None
        if item is not None:
            plan = (await self.session.execute(
                select(Plan).where(Plan.product == row.product, Plan.code == tier)
            )).scalar_one_or_none()
        if plan is None:
            raise ActivationError("stripe_mismatch", org_id=org_id)
        status = UAM_STATUS.get(_at(stripe_sub, "status"))
        if status is None:
            raise ActivationError("unknown_status", org_id=org_id)

        row.status = status
        row.plan_id = plan.id
        # Since Stripe's 2025 API versions the period end lives on the item; older versions put it on the subscription.
        period_end = _at(item, "current_period_end") or _at(stripe_sub, "current_period_end")
        if period_end:
            row.current_period_end = datetime.fromtimestamp(period_end, tz=timezone.utc)
        elif replacement:
            row.current_period_end = None
        flag = _at(stripe_sub, "cancel_at_period_end")
        if flag is not None:
            row.cancel_at_period_end = bool(flag)
        elif replacement:
            row.cancel_at_period_end = False

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

        plan_result = await self.session.execute(
            select(Plan).where(Plan.product == product, Plan.code == tier)
        )
        plan = plan_result.scalar_one_or_none()
        if not plan:
            raise ActivationError("unknown_plan", org_id=org_id_str)

        # BUG-08: lock, then check ownership and conflict on the refreshed rows before any change.
        org, subscription = await self._lock(org.id, product)
        if org.stripe_customer_id and org.stripe_customer_id != stripe_customer_id:
            raise ActivationError("customer_mismatch", org_id=org_id_str)
        if (subscription and subscription.stripe_subscription_id != stripe_subscription_id
                and subscription.status != "cancelled"):
            # A second live subscription for one product: loud, until an operator acts (CR-214 ruling 8).
            raise ActivationError("conflicting_subscription", org_id=org_id_str)
        replacement = subscription is None or subscription.stripe_subscription_id != stripe_subscription_id
        stripe_sub = self._retrieve(stripe_subscription_id, org.id)

        org.stripe_customer_id = stripe_customer_id
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
            subscription.source = "stripe"
            subscription.stripe_customer_id = stripe_customer_id
            subscription.stripe_subscription_id = stripe_subscription_id
        await self._reconcile(subscription, stripe_sub, replacement)

        await self.session.flush()
        await self.audit_service.log_event(
            action="billing_subscription_activated",
            org_id=str(org.id),
            metadata={"product": product, "tier": tier, "stripe_subscription_id": stripe_subscription_id}
        )

    async def _known_subscription(self, data_object: dict) -> Subscription:
        """Locate the event's subscription (without changing it), lock per BUG-08, and recheck it is still ours."""
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
        _, subscription = await self._lock(subscription.org_id, subscription.product)
        if not subscription or subscription.stripe_subscription_id != stripe_subscription_id:
            raise ActivationError("unknown_subscription")
        return subscription

    async def _update(self, data_object: dict) -> None:
        subscription = await self._known_subscription(data_object)
        # BUG-08: Stripe's current state, never the event body (events arrive out of order and are retried).
        stripe_sub = self._retrieve(subscription.stripe_subscription_id, subscription.org_id)
        await self._reconcile(subscription, stripe_sub, replacement=False)
        await self.audit_service.log_event(
            action="billing_subscription_updated",
            org_id=str(subscription.org_id),
            metadata={"stripe_subscription_id": data_object["id"], "status": subscription.status}
        )

    async def _cancel(self, data_object: dict) -> None:
        subscription = await self._known_subscription(data_object)
        stripe_sub = self._retrieve(subscription.stripe_subscription_id, subscription.org_id)
        await self._reconcile(subscription, stripe_sub, replacement=False)
        await self.audit_service.log_event(
            action="billing_subscription_cancelled",
            org_id=str(subscription.org_id),
            metadata={"stripe_subscription_id": data_object["id"], "status": subscription.status}
        )
