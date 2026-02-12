from __future__ import annotations

from fastapi.responses import JSONResponse
from fastapi import Request


class AppError(Exception):
    def __init__(self, detail: str, status_code: int = 400, code: str = "error"):
        self.detail = detail
        self.status_code = status_code
        self.code = code
        super().__init__(detail)


class AuthError(AppError):
    def __init__(self, detail: str = "Authentication failed", code: str = "auth_failed"):
        super().__init__(detail, status_code=401, code=code)


class ForbiddenError(AppError):
    def __init__(self, detail: str = "Forbidden", code: str = "forbidden"):
        super().__init__(detail, status_code=403, code=code)


class NotFoundError(AppError):
    def __init__(self, detail: str = "Not found", code: str = "not_found"):
        super().__init__(detail, status_code=404, code=code)


class ConflictError(AppError):
    def __init__(self, detail: str = "Conflict", code: str = "conflict"):
        super().__init__(detail, status_code=409, code=code)


class ValidationError(AppError):
    def __init__(self, detail: str = "Validation error", code: str = "validation_error"):
        super().__init__(detail, status_code=422, code=code)


class RateLimitError(AppError):
    def __init__(self, detail: str = "Rate limit exceeded", code: str = "rate_limited"):
        super().__init__(detail, status_code=429, code=code)


def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.detail}},
    )
