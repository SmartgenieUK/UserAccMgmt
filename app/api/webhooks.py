from __future__ import annotations

import logging

import stripe
from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings, Settings
from app.core.exceptions import ActivationError, AppError
from app.db.session import get_session
from app.services.audit_service import AuditService
from app.services.stripe_gateway import StripeGateway, get_stripe_gateway
from app.services.billing_service import BillingService

router = APIRouter(include_in_schema=False)
logger = logging.getLogger(__name__)


def get_billing_service(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    gateway: StripeGateway = Depends(get_stripe_gateway),
) -> BillingService:
    return BillingService(session, settings, AuditService(session, settings), gateway)


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    service: BillingService = Depends(get_billing_service),
    gateway: StripeGateway = Depends(get_stripe_gateway),
    session: AsyncSession = Depends(get_session),
):
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = gateway.construct_event(payload, sig_header)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        raise AppError(detail=str(e), status_code=400, code="invalid_signature")

    try:
        await service.handle_event(event)
    except ActivationError as e:
        # Nothing is recorded, so Stripe's retry is processed afresh; Stripe alerts on a failing endpoint.
        await session.rollback()
        logger.error(
            "stripe event not applied: event_id=%s type=%s reason=%s org_id=%s",
            event.get("id"), event.get("type"), e.reason, e.org_id or "-",
        )
        raise
    await session.commit()
    
    return {"received": True}
