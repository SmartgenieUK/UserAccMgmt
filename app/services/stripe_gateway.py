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
        )
        return session.url

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
