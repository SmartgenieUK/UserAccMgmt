from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings
from app.db.redis import get_redis
from app.services.rate_limit_service import RateLimiter


class GlobalRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        settings = get_settings()
        limiter = RateLimiter(get_redis(request))
        ip = request.client.host if request.client else "unknown"
        await limiter.hit(f"global:{ip}", settings.RATE_LIMIT_GLOBAL_PER_MINUTE, 60)
        return await call_next(request)
