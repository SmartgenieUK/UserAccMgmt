from __future__ import annotations

from typing import Any
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.exceptions import AppError


async def init_redis(settings: Settings, app: Any) -> None:
    if not settings.REDIS_URL:
        if settings.REDIS_REQUIRED:
            raise AppError("Redis is required but REDIS_URL is not set", status_code=500, code="redis_required")
        app.state.redis = None
        return

    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await redis.ping()
    except Exception as exc:  # pragma: no cover
        if settings.REDIS_REQUIRED:
            raise AppError("Redis connection failed", status_code=500, code="redis_unavailable") from exc
        redis = None
    app.state.redis = redis


async def close_redis(app: Any) -> None:
    redis = getattr(app.state, "redis", None)
    if redis is not None:
        await redis.close()


def get_redis(request: Any) -> Redis | None:
    return getattr(request.app.state, "redis", None)
