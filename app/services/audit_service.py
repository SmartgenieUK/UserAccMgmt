from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import AuditEvent
from app.utils.time import utcnow


class AuditService:
    def __init__(self, session: AsyncSession, settings: Settings):
        self.session = session
        self.settings = settings

    async def log_event(
        self,
        action: str,
        user_id: str | None = None,
        org_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        if not self.settings.AUDIT_LOG_ENABLED:
            return
        event = AuditEvent(
            user_id=user_id,
            org_id=org_id,
            action=action,
            ip_address=ip_address,
            user_agent=user_agent,
            event_metadata=metadata or {},
            created_at=utcnow(),
        )
        self.session.add(event)
