from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings
from app.db.redis import get_redis
from app.services.rate_limit_service import RateLimiter


# The Entra token-issuance callout is a Tier-0 sign-in dependency called from Entra's own IPs, not end
# users; the per-IP global limiter must never throttle it or it would break sign-in tenant-wide (W3.3).
# Stripe webhooks come from Stripe IPs and also must not be rate limited per-IP.
_RATE_LIMIT_EXEMPT = ("/api/v1/entra/token-issuance", "/webhooks/stripe")


class GlobalRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in _RATE_LIMIT_EXEMPT:
            return await call_next(request)
        settings = get_settings()
        limiter = RateLimiter(get_redis(request))
        ip = request.client.host if request.client else "unknown"
        await limiter.hit(f"global:{ip}", settings.RATE_LIMIT_GLOBAL_PER_MINUTE, 60)
        return await call_next(request)
