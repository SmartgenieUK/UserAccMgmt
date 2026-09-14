from __future__ import annotations

import stripe
from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings, Settings
from app.core.exceptions import AppError
from app.db.session import get_session
from app.services.audit_service import AuditService
from app.services.stripe_gateway import StripeGateway, get_stripe_gateway
from app.services.billing_service import BillingService

router = APIRouter(include_in_schema=False)


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

    await service.handle_event(event)
    await session.commit()
    
    return {"received": True}
