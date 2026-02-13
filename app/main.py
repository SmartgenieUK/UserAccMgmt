from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.api import api_router
from app.api.web import router as web_router
from app.core.config import get_settings
from app.core.exceptions import app_error_handler, AppError
from app.core.logging import setup_logging
from app.db.redis import init_redis, close_redis
from app.middleware.request_id import RequestIdMiddleware
from app.middleware.logging import LoggingMiddleware
from app.middleware.tenant import TenantContextMiddleware
from app.middleware.rate_limit import GlobalRateLimitMiddleware
from app.middleware.metrics import MetricsMiddleware

settings = get_settings()
setup_logging(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_redis(settings, app)
    yield
    await close_redis(app)


app = FastAPI(
    title=settings.APP_NAME,
    openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
    docs_url=f"{settings.API_V1_PREFIX}/docs",
    redoc_url=f"{settings.API_V1_PREFIX}/redoc",
    lifespan=lifespan,
)

app.add_exception_handler(AppError, app_error_handler)

app.add_middleware(RequestIdMiddleware)
app.add_middleware(LoggingMiddleware)
app.add_middleware(TenantContextMiddleware)

if settings.METRICS_ENABLED:
    app.add_middleware(MetricsMiddleware)

app.add_middleware(GlobalRateLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(web_router)
app.include_router(api_router, prefix=settings.API_V1_PREFIX)
