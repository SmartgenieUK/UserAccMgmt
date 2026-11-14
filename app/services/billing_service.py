from __future__ import annotations

import uuid
from calendar import monthrange
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import stripe
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.config import Settings
from app.core.exceptions import ActivationError, AppError, ConflictError, ValidationError
from app.models import Organization, Plan, Subscription, StripeEvent, TesterRedemption, User
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
        if price_id and price_id == self.settings.STRIPE_TESTER_PRICE_ID:
            return "devgenie" if price_id not in self.settings.STRIPE_PRICE_MAP else None
        target = self.settings.STRIPE_PRICE_MAP.get(price_id) if price_id else None
        return target.split(":", 1)[0] if target else None

    def _product_item(self, stripe_sub, product: str):
        """The subscription item whose price maps to this product, and its tier; (None, None) when none does."""
        for item in _at(_at(stripe_sub, "items"), "data") or []:
            price_id = _at(_at(item, "price"), "id")
            if self._product_of(price_id) == product:
                tier = "pro" if price_id == self.settings.STRIPE_TESTER_PRICE_ID else self.settings.STRIPE_PRICE_MAP[price_id].split(":", 1)[1]
                return item, tier
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
        if price_id == self.settings.STRIPE_TESTER_PRICE_ID or price_id not in self.settings.STRIPE_PRICE_MAP:
            raise ValidationError("Unknown price id", code="unknown_price")
        product = self._product_of(price_id)

        # BUG-08: one live subscription per org and product. Lock, refuse a live one, reuse an open session.
        org, local = await self._lock(org.id, product)
        if product == "devgenie" and (await self.session.execute(
            select(TesterRedemption.id).where(TesterRedemption.org_id == org.id, TesterRedemption.state.in_(("reserved", "pending")))
        )).first():
            raise ConflictError("Tester redemption is pending", code="subscription_exists")
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

    # BRIEF-184: the account lock always precedes the product/org lock. The unique constraints
    # additionally protect one use per user and per org across different memberships.
    async def _tester_lock(self, user_id, org_id):
        await self.session.execute(
            select(User).where(User.id == user_id).with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self._lock(org_id, "devgenie")

    async def _tester_reservation(self, user_id, org_id):
        rows = (await self.session.execute(
            select(TesterRedemption).where(or_(
                TesterRedemption.user_id == user_id, TesterRedemption.org_id == org_id,
            )).execution_options(populate_existing=True)
        )).scalars().all()
        for row in rows:
            if (row.state not in ("reserved", "pending") or str(row.user_id) != str(user_id)
                    or str(row.org_id) != str(org_id)):
                raise ConflictError("Tester month already used", code="tester_already_used")
        return rows[0] if rows else None

    @staticmethod
    def _tester_unavailable():
        return AppError("Tester redemption needs reconciliation", status_code=502, code="stripe_unavailable")

    def _tester_price(self, expected_id=None, require_active=True):
        price_id = self.settings.STRIPE_TESTER_PRICE_ID
        if (not price_id or price_id in self.settings.STRIPE_PRICE_MAP
                or (expected_id is not None and expected_id != price_id)):
            raise ValidationError("Tester redemption is unavailable", code="invalid_tester_code")
        price = self.gateway.retrieve_price(price_id)
        recurring = _at(price, "recurring")
        amount = _at(price, "unit_amount")
        decimal = _at(price, "unit_amount_decimal")
        try:
            decimal_zero = decimal is None or (isinstance(decimal, str) and Decimal(decimal) == 0)
        except InvalidOperation:
            decimal_zero = False
        if (not isinstance(_at(price, "product"), str) or not _at(price, "product")
                or _at(price, "id") != price_id or (require_active and _at(price, "active") is not True)
                or _at(price, "currency") != "gbp" or type(amount) is not int or amount != 0
                or not decimal_zero or _at(price, "billing_scheme") != "per_unit"
                or _at(price, "type") != "recurring" or _at(recurring, "interval") != "month"
                or type(_at(recurring, "interval_count")) is not int or _at(recurring, "interval_count") != 1
                or _at(recurring, "usage_type") != "licensed" or _at(price, "transform_quantity") is not None):
            raise ValidationError("Invalid tester price", code="invalid_tester_code")
        return price

    def _tester_promotion(self, promotion, price, customer_id, now):
        invalid = ValidationError("Invalid tester code", code="invalid_tester_code")
        if (not isinstance(_at(promotion, "id"), str) or not _at(promotion, "id")
                or _at(promotion, "active") is not True
                or _at(_at(promotion, "metadata"), "tester") != "true"):
            raise invalid
        expiry = _at(promotion, "expires_at")
        cap, count = _at(promotion, "max_redemptions"), _at(promotion, "times_redeemed")
        if type(expiry) is not int or expiry <= 0:
            raise invalid
        if expiry <= now.timestamp():
            raise ValidationError("Tester code expired", code="tester_code_expired")
        if type(cap) is not int or cap <= 0 or type(count) is not int or count < 0:
            raise invalid
        if count >= cap:
            raise ValidationError("Tester code exhausted", code="tester_code_exhausted")
        restricted_customer = _at(promotion, "customer")
        if restricted_customer is not None and restricted_customer != customer_id:
            raise invalid
        restrictions = _at(promotion, "restrictions")
        if restrictions is not None and not isinstance(restrictions, Mapping):
            raise invalid
        if (_at(restrictions, "minimum_amount") is not None
                or _at(restrictions, "minimum_amount_currency") is not None
                or _at(restrictions, "currency_options") not in (None, {})
                or (_at(restrictions, "first_time_transaction") is not None
                    and _at(restrictions, "first_time_transaction") is not False)):
            raise invalid
        coupon = _at(promotion, "coupon")
        if isinstance(coupon, str):
            coupon = self.gateway.retrieve_coupon(coupon)
        if _at(coupon, "valid") is not True or not _at(coupon, "id"):
            raise invalid
        applies = _at(coupon, "applies_to")
        if applies is not None:
            products = _at(applies, "products")
            if not isinstance(products, list) or _at(price, "product") not in products:
                raise invalid
        return promotion

    def _tester_customer_safe(self, customer_id):
        # Refuse balances/pending charges that could turn the zero-price first invoice nonzero.
        # No attempt is made to change a customer's saved cards or billing settings.
        customer = self.gateway.retrieve_customer(customer_id)
        balance = _at(customer, "balance")
        credits = _at(customer, "invoice_credit_balance")
        cash = _at(_at(customer, "cash_balance"), "available")
        def zero_balances(values):
            return values is None or (isinstance(values, Mapping)
                                      and all(type(v) is int and v == 0 for v in values.values()))
        if (_at(customer, "id") != customer_id or _at(customer, "deleted")
                or type(balance) is not int or balance != 0
                or not zero_balances(credits) or not zero_balances(cash)
                or self.gateway.list_pending_invoice_items(customer_id)):
            raise self._tester_unavailable()

    async def _tester_conflicts(self, org, local, now, own_subscription_id=None):
        if (local and (own_subscription_id is None or local.stripe_subscription_id != own_subscription_id)
                and local.status in LOCAL_LIVE_STATUSES
                and (local.stripe_subscription_id or local.current_period_end is None
                     or local.current_period_end > now)):
            raise ConflictError("A DevGenie plan exists", code="subscription_exists")
        if not org.stripe_customer_id:
            return
        sessions = self.gateway.list_open_checkout_sessions(org.stripe_customer_id)
        subscriptions = self.gateway.list_subscriptions(org.stripe_customer_id)
        for sub in subscriptions:
            if (_at(sub, "id") != own_subscription_id and _at(sub, "status") in LIVE_STRIPE_STATUSES
                    and self._product_item(sub, "devgenie")[0] is not None):
                raise ConflictError("A DevGenie plan exists", code="subscription_exists")
        for checkout in sessions:
            if self._product_of(_at(_at(checkout, "metadata"), "price_id")) == "devgenie":
                raise ConflictError("A checkout is in progress", code="subscription_exists")

    def _tester_confirm(self, reservation, sub):
        """Verify the external effect, not current code eligibility (a successful code may be spent)."""
        items = _at(_at(sub, "items"), "data")
        discounts = _at(sub, "discounts")
        if not discounts:
            discount = _at(sub, "discount")
            discounts = [discount] if discount else []
        invoice = _at(sub, "latest_invoice")
        if isinstance(invoice, str):
            invoice = self.gateway.retrieve_invoice(invoice)
        if (not isinstance(_at(sub, "id"), str) or not _at(sub, "id")
                or _at(sub, "customer") != reservation.stripe_customer_id
                or (reservation.stripe_subscription_id is not None
                    and _at(sub, "id") != reservation.stripe_subscription_id)
                or _at(_at(sub, "metadata"), "tester_redemption_id") != str(reservation.id)
                or not isinstance(items, list) or len(items) != 1
                or _at(_at(items[0], "price"), "id") != reservation.tester_price_id
                or _at(items[0], "quantity") not in (None, 1)
                or _at(sub, "cancel_at") != int(reservation.ends_at.timestamp())
                or not isinstance(discounts, list) or len(discounts) != 1
                or _at(discounts[0], "promotion_code") != reservation.promotion_code_id
                or _at(sub, "collection_method") != "send_invoice"
                or _at(sub, "default_payment_method") or _at(sub, "default_source")
                or _at(_at(sub, "automatic_tax"), "enabled")):
            raise self._tester_unavailable()
        self._tester_hold_invoice(reservation, sub["id"], invoice)

    @staticmethod
    def _invoice_subscription(invoice):
        # Basil moved the subscription reference into parent.subscription_details.
        return (_at(_at(_at(invoice, "parent"), "subscription_details"), "subscription")
                or _at(invoice, "subscription"))

    def _tester_hold_invoice(self, reservation, subscription_id, invoice):
        invoice_id = _at(invoice, "id")

        def own_draft(value):
            return (isinstance(invoice_id, str) and bool(invoice_id)
                    and _at(value, "id") == invoice_id
                    and _at(value, "customer") == reservation.stripe_customer_id
                    and self._invoice_subscription(value) == subscription_id
                    and _at(value, "status") == "draft")

        if not own_draft(invoice):
            raise self._tester_unavailable()
        if _at(invoice, "auto_advance") is not False:
            invoice = self.gateway.hold_tester_invoice(invoice_id)
        # Confirm Stripe's reply before granting access. Hold even an unexpectedly nonzero draft
        # first, so a failed zero-value check cannot leave an invoice scheduled to be sent.
        if (not own_draft(invoice) or _at(invoice, "auto_advance") is not False
                or _at(invoice, "currency") != "gbp"
                or any(type(_at(invoice, key)) is not int or _at(invoice, key) != 0
                       for key in ("total", "subtotal", "amount_due", "amount_paid", "amount_remaining"))
                or _at(invoice, "charge") or _at(invoice, "payment_intent")):
            raise self._tester_unavailable()

    async def _tester_finish(self, org, local, reservation, sub, now):
        self._tester_confirm(reservation, sub)
        status = _at(sub, "status")
        if status in ("canceled", "incomplete_expired") or reservation.ends_at <= now:
            # Consume even after cancellation/expiry. Do not touch a later local paid subscription.
            reservation.stripe_subscription_id = sub["id"]
            reservation.state = "applied"
            await self.session.commit()
            raise ConflictError("Tester month already used", code="tester_already_used")
        if status != "active":
            raise self._tester_unavailable()
        if org.stripe_customer_id != reservation.stripe_customer_id:
            raise self._tester_unavailable()
        await self._tester_conflicts(org, local, now, sub["id"])
        plan = (await self.session.execute(
            select(Plan).where(Plan.product == "devgenie", Plan.code == "pro")
        )).scalar_one_or_none()
        if plan is None:
            raise self._tester_unavailable()
        if local is None:
            local = Subscription(org_id=org.id, product="devgenie", plan_id=plan.id,
                                 status="active", source="stripe")
            self.session.add(local)
        local.plan_id = plan.id
        local.source = "stripe"
        local.status = "active"
        local.seats = None
        local.stripe_customer_id = reservation.stripe_customer_id
        local.stripe_subscription_id = sub["id"]
        local.current_period_end = reservation.ends_at
        local.cancel_at_period_end = True
        reservation.stripe_subscription_id = sub["id"]
        reservation.state = "applied"
        end = reservation.ends_at
        await self.session.commit()
        return {"status": "active", "tier": "pro", "ends_at": end.isoformat()}

    async def _tester_resume(self, org, local, reservation):
        # Durable state, not the calling request, proves whether a write has been attempted.
        fresh = reservation.state == "reserved"
        now = datetime.now(timezone.utc)
        # Find our own external write before code expiry/cap checks or live-plan conflict checks.
        # A lost response has no local subscription id, so scan every page for server-written metadata.
        subscriptions = None
        if reservation.stripe_subscription_id:
            sub = self.gateway.retrieve_tester_subscription(reservation.stripe_subscription_id)
        else:
            subscriptions = self.gateway.list_subscriptions(reservation.stripe_customer_id)
            matches = [s for s in subscriptions
                       if _at(_at(s, "metadata"), "tester_redemption_id") == str(reservation.id)]
            if len(matches) > 1:
                raise self._tester_unavailable()
            sub = self.gateway.retrieve_tester_subscription(matches[0]["id"]) if matches else None
        if sub is not None:
            # Configuration must still identify the reserved zero price, but code eligibility is irrelevant.
            try:
                self._tester_price(reservation.tester_price_id, require_active=False)
            except ValidationError:
                raise self._tester_unavailable() from None
            return await self._tester_finish(org, local, reservation, sub, now)
        # An empty read is NOT proof of non-creation. Only replay the original key/payload during
        # Stripe's minimum 24-hour retention. After that an operator must reconcile; never mint a key.
        if not fresh and (not timedelta(0) <= now - reservation.started_at < timedelta(hours=24)
                          or now >= reservation.ends_at):
            raise self._tester_unavailable()
        try:
            if now >= reservation.ends_at:
                raise ValidationError("Tester reservation expired", code="tester_code_expired")
            price = self._tester_price(reservation.tester_price_id)
            promotion = self.gateway.retrieve_promotion_code(reservation.promotion_code_id)
            self._tester_promotion(promotion, price, reservation.stripe_customer_id, now)
        except ValidationError:
            # A reserved row proves no request has attempted the external write. Commit its release
            # before returning a definite refusal, so a fresh request can start paid checkout.
            # An uncertain retry cannot use an empty lookup as proof of non-creation.
            if fresh:
                await self.session.delete(reservation)
                await self.session.commit()
                raise
            raise self._tester_unavailable() from None
        if org.stripe_customer_id != reservation.stripe_customer_id:
            raise self._tester_unavailable()
        await self._tester_conflicts(org, local, now)
        self._tester_customer_safe(reservation.stripe_customer_id)
        if fresh:
            # Persist uncertainty BEFORE calling Stripe. A retry may overtake this request during
            # either commit/relock; it must never release a reservation whose write was attempted.
            user_id, org_id = reservation.user_id, reservation.org_id
            reservation.state = "pending"
            await self.session.commit()
            org, local = await self._tester_lock(user_id, org_id)
            reservation = await self._tester_reservation(user_id, org_id)
            if reservation is None:
                raise self._tester_unavailable()
        try:
            sub = self.gateway.create_tester_subscription(
                customer_id=reservation.stripe_customer_id,
                price_id=reservation.tester_price_id,
                promotion_code_id=reservation.promotion_code_id,
                cancel_at=int(reservation.ends_at.timestamp()),
                redemption_id=str(reservation.id),
                idempotency_key="tester-redemption-" + str(reservation.id),
            )
        except stripe.error.InvalidRequestError as exc:
            # A definite discount rejection cannot have created the subscription. All other errors,
            # including timeouts and idempotency conflicts, preserve pending for reconciliation.
            if exc.param != "discounts":
                raise
            promotion = self.gateway.retrieve_promotion_code(reservation.promotion_code_id)
            try:
                self._tester_promotion(promotion, price, reservation.stripe_customer_id, now)
            except ValidationError as refusal:
                await self.session.delete(reservation)
                await self.session.commit()
                raise refusal from None
            raise self._tester_unavailable() from None
        return await self._tester_finish(org, local, reservation, sub, datetime.now(timezone.utc))

    async def redeem_tester_code(self, org: Organization, code: str, actor_user_id: str, actor_email: str) -> dict:
        org_id = org.id
        try:
            org, local = await self._tester_lock(actor_user_id, org_id)
            reservation = await self._tester_reservation(actor_user_id, org_id)
            if reservation is not None:
                return await self._tester_resume(org, local, reservation)
            now = datetime.now(timezone.utc)
            price = self._tester_price()
            matches = [p for p in self.gateway.list_promotion_codes(code)
                       if isinstance(_at(p, "code"), str) and p["code"].casefold() == code.casefold()]
            eligible, refusals = [], []
            for candidate in matches:
                try:
                    eligible.append(self._tester_promotion(candidate, price, org.stripe_customer_id, now))
                except ValidationError as refusal:
                    refusals.append(refusal)
            if len(eligible) != 1:
                if len(matches) == 1 and refusals:
                    raise refusals[0]
                raise ValidationError("Invalid tester code", code="invalid_tester_code")
            promotion = eligible[0]
            await self._tester_conflicts(org, local, now)
            customer_id = await self.ensure_customer(org, actor_email)
            self._tester_customer_safe(customer_id)
            year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
            end = now.replace(year=year, month=month, day=min(now.day, monthrange(year, month)[1]))
            reservation = TesterRedemption(
                id=uuid.uuid4(), user_id=actor_user_id, org_id=org_id,
                promotion_code_id=promotion["id"], tester_price_id=price["id"],
                stripe_customer_id=customer_id, started_at=now, ends_at=end, state="reserved",
            )
            self.session.add(reservation)
            # Reserve before rechecking eligibility; pending is recorded separately before any write.
            await self.session.commit()
            org, local = await self._tester_lock(actor_user_id, org_id)
            reservation = await self._tester_reservation(actor_user_id, org_id)
            if reservation is None:
                raise self._tester_unavailable()
            return await self._tester_resume(org, local, reservation)
        except (stripe.error.StripeError, SQLAlchemyError):
            await self.session.rollback()
            raise self._tester_unavailable() from None

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
        elif event_type == "invoice.created":
            await self._hold_created_tester_invoice(data_object)

        self.session.add(StripeEvent(event_id=event_id, type=event_type))
        try:
            await self.session.flush()
        except IntegrityError:
            # A concurrent delivery of the same event applied it first; drop ours.
            await self.session.rollback()

    async def _hold_created_tester_invoice(self, data_object):
        # A create response may be lost before the synchronous invoice hold. Secure the draft
        # on webhook delivery too, without waiting for a user retry or granting entitlement.
        customer_id, invoice_id = _at(data_object, "customer"), _at(data_object, "id")
        if not customer_id or not invoice_id:
            return
        reservations = (await self.session.execute(
            select(TesterRedemption).where(TesterRedemption.stripe_customer_id == customer_id)
        )).scalars().all()
        if not reservations:
            return
        try:
            invoice = self.gateway.retrieve_invoice(invoice_id)
            subscription_id = self._invoice_subscription(invoice)
            if not subscription_id:
                return  # An ordinary standalone invoice.
            sub = self.gateway.retrieve_tester_subscription(subscription_id)
            reservation = next((r for r in reservations
                                if str(r.id) == _at(_at(sub, "metadata"), "tester_redemption_id")), None)
            if reservation is None:
                return  # A different subscription, including a later paid replacement.
            self._tester_confirm(reservation, {**sub, "latest_invoice": invoice})
        except (stripe.error.StripeError, AppError):
            # Do not acknowledge an unconfirmed hold; Stripe must retry this event.
            raise ActivationError("stripe_unavailable", org_id=str(reservations[0].org_id)) from None

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

        previous_status = row.status
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

        tester = (await self.session.execute(
            select(TesterRedemption).where(
                TesterRedemption.org_id == row.org_id,
                TesterRedemption.stripe_subscription_id == row.stripe_subscription_id,
                TesterRedemption.state == "applied",
            )
        )).scalar_one_or_none()
        if tester is not None:
            if (row.product != "devgenie" or tester.stripe_customer_id != row.stripe_customer_id
                    or tester.tester_price_id != self.settings.STRIPE_TESTER_PRICE_ID
                    or tester.tester_price_id in self.settings.STRIPE_PRICE_MAP
                    or _at(_at(item, "price"), "id") != tester.tester_price_id):
                raise ActivationError("stripe_mismatch", org_id=org_id)
            # cancel_at is independent of Stripe's cancel_at_period_end. Keep our visible ending
            # flag and durable upper bound even if the authoritative Stripe reply omits/extends it.
            row.current_period_end = min(row.current_period_end, tester.ends_at) if row.current_period_end else tester.ends_at
            row.cancel_at_period_end = True
            if previous_status == "cancelled" or tester.ends_at <= datetime.now(timezone.utc):
                row.status = "cancelled"

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
