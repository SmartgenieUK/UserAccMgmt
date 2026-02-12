from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request, session: AsyncSession = Depends(get_session)):
    await session.execute(text("SELECT 1"))
    redis = getattr(request.app.state, "redis", None)
    if redis:
        await redis.ping()
    return {"status": "ready"}
