from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.users import router as users_router
from app.api.v1.orgs import router as orgs_router
from app.api.v1.admin import router as admin_router
from app.api.v1.oauth import router as oauth_router
from app.api.v1.health import router as health_router

api_router = APIRouter()
api_router.include_router(auth_router, tags=["auth"])
api_router.include_router(oauth_router, tags=["oauth"])
api_router.include_router(users_router, tags=["users"])
api_router.include_router(orgs_router, tags=["orgs"])
api_router.include_router(admin_router, tags=["admin"])
api_router.include_router(health_router, tags=["health"])
