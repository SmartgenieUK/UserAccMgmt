from __future__ import annotations

import time

from redis.asyncio import Redis

from app.core.exceptions import RateLimitError


class InMemoryRateLimiter:
    def __init__(self):
        self._store: dict[str, tuple[int, float]] = {}

    async def hit(self, key: str, limit: int, period_seconds: int) -> None:
        now = time.time()
        count, reset_at = self._store.get(key, (0, now + period_seconds))
        if now > reset_at:
            count, reset_at = 0, now + period_seconds
        count += 1
        self._store[key] = (count, reset_at)
        if count > limit:
            raise RateLimitError()


class RateLimiter:
    def __init__(self, redis: Redis | None):
        self.redis = redis
        self.memory = InMemoryRateLimiter()

    async def hit(self, key: str, limit: int, period_seconds: int) -> None:
        if not self.redis:
            await self.memory.hit(key, limit, period_seconds)
            return
        count = await self.redis.incr(key)
        if count == 1:
            await self.redis.expire(key, period_seconds)
        if count > limit:
            raise RateLimitError()
