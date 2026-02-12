from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.utils.context import org_id_ctx


class TenantContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        org_id = request.headers.get("X-Org-Id")
        if org_id:
            org_id_ctx.set(org_id)
        response = await call_next(request)
        return response
