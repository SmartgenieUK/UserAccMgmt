from __future__ import annotations

import stripe
from fastapi import Depends

from app.core.config import Settings, get_settings


class StripeGateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        stripe.api_key = settings.STRIPE_API_KEY

    def create_customer(self, name: str, email: str, metadata: dict) -> str:
        # The email is where Stripe sends receipts and invoices (BRIEF-019).
        customer = stripe.Customer.create(name=name, email=email, metadata=metadata)
        return customer.id

    def create_checkout_session(
        self, customer_id: str, price_id: str, success_url: str, cancel_url: str, metadata: dict
    ) -> str:
        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            subscription_data={"metadata": metadata},
            metadata=metadata,
            # Stripe's promotion-code box; codes and their rules live in the Stripe dashboard (BRIEF-019). No
            # payment_method_collection: a card is collected even on a 100%-off code.
            allow_promotion_codes=True,
            # Cards only, so a checkout completes paid or unpaid at once: no delayed-payment events (BUG-08).
            payment_method_types=["card"],
        )
        return session.url

    # BUG-08: Stripe's current state. Each returns top-level dicts (to_dict() is shallow: read nested values by
    # key) and raises on a Stripe error.
    def retrieve_subscription(self, subscription_id: str) -> dict:
        return stripe.Subscription.retrieve(subscription_id).to_dict()

    def list_subscriptions(self, customer_id: str) -> list[dict]:
        listing = stripe.Subscription.list(customer=customer_id, status="all", limit=100)
        return [sub.to_dict() for sub in listing.auto_paging_iter()]

    def list_open_checkout_sessions(self, customer_id: str) -> list[dict]:
        listing = stripe.checkout.Session.list(customer=customer_id, status="open", limit=100)
        return [{"url": s["url"], "metadata": dict(s["metadata"] or {})} for s in listing.auto_paging_iter()]

    # BRIEF-184: read-only eligibility/recovery seam. Pagination is required for both codes and subscriptions.
    def retrieve_tester_subscription(self, subscription_id: str) -> dict:
        # Discounts are ids unless explicitly expanded; recovery must verify the applied promotion.
        return stripe.Subscription.retrieve(
            subscription_id, expand=["latest_invoice", "discounts"],
        ).to_dict()

    def retrieve_price(self, price_id: str) -> dict:
        return stripe.Price.retrieve(price_id).to_dict()

    def list_promotion_codes(self, code: str) -> list[dict]:
        listing = stripe.PromotionCode.list(code=code, limit=100, expand=["data.coupon"])
        return [promotion.to_dict() for promotion in listing.auto_paging_iter()]

    def retrieve_promotion_code(self, promotion_code_id: str) -> dict:
        return stripe.PromotionCode.retrieve(promotion_code_id, expand=["coupon"]).to_dict()

    def retrieve_coupon(self, coupon_id: str) -> dict:
        return stripe.Coupon.retrieve(coupon_id).to_dict()

    def retrieve_customer(self, customer_id: str) -> dict:
        return stripe.Customer.retrieve(customer_id).to_dict()

    def list_pending_invoice_items(self, customer_id: str) -> list[dict]:
        listing = stripe.InvoiceItem.list(customer=customer_id, pending=True, limit=100)
        return [item.to_dict() for item in listing.auto_paging_iter()]

    def retrieve_invoice(self, invoice_id: str) -> dict:
        return stripe.Invoice.retrieve(invoice_id).to_dict()

    def create_tester_subscription(
        self, customer_id: str, price_id: str, promotion_code_id: str,
        cancel_at: int, redemption_id: str, idempotency_key: str,
    ) -> dict:
        # Manual collection prevents fallback charging a customer's saved card. Never attach, select,
        # save or charge a payment method on this path. V0 sandbox feasibility remains a release gate.
        subscription = stripe.Subscription.create(
            customer=customer_id,
            items=[{"price": price_id, "quantity": 1}],
            discounts=[{"promotion_code": promotion_code_id}],
            cancel_at=cancel_at,
            collection_method="send_invoice",
            days_until_due=1,
            payment_settings={"save_default_payment_method": "off"},
            automatic_tax={"enabled": False},
            metadata={"tester_redemption_id": redemption_id},
            expand=["latest_invoice", "discounts"],
            idempotency_key=idempotency_key,
        )
        return subscription.to_dict()

    def create_portal_session(self, customer_id: str, return_url: str, cancel_subscription_id: str | None = None) -> str:
        params = {"customer": customer_id, "return_url": return_url}
        if cancel_subscription_id:
            # Straight to Stripe's cancel confirmation, then back to the same page (BRIEF-029).
            params["flow_data"] = {
                "type": "subscription_cancel",
                "subscription_cancel": {"subscription": cancel_subscription_id},
                "after_completion": {"type": "redirect", "redirect": {"return_url": return_url}},
            }
        session = stripe.billing_portal.Session.create(**params)
        return session.url

    def construct_event(self, payload: bytes, sig_header: str) -> dict:
        event = stripe.Webhook.construct_event(
            payload, sig_header, self.settings.STRIPE_WEBHOOK_SECRET
        )
        # return the event as a dict
        return event.to_dict()


def get_stripe_gateway(settings: Settings = Depends(get_settings)) -> StripeGateway:
    return StripeGateway(settings)
