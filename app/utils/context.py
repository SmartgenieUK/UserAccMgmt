from __future__ import annotations

import contextvars

request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
org_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("org_id", default=None)
