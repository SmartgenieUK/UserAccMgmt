from __future__ import annotations

import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import NotFoundError, ConflictError, AuthError
from app.models import Application, Organization
from app.security.hashing import hash_token, verify_token


class ApplicationService:
    def __init__(self, session: AsyncSession, settings: Settings):
        self.session = session
        self.settings = settings

    @staticmethod
    def _generate_client_id() -> str:
        return f"cg_{secrets.token_urlsafe(24)}"

    @staticmethod
    def _generate_client_secret() -> str:
        return f"cgs_{secrets.token_urlsafe(48)}"

    async def create(self, org_id: str, name: str, redirect_uris: list[str], allowed_scopes: list[str]) -> tuple[Application, str]:
        org = await self.session.get(Organization, org_id)
        if not org:
            raise NotFoundError("Organization not found", code="org_not_found")

        client_id = self._generate_client_id()
        client_secret = self._generate_client_secret()
        secret_hash = hash_token(client_secret)

        app = Application(
            org_id=uuid.UUID(org_id),
            name=name,
            client_id=client_id,
            secret_hash=secret_hash,
            redirect_uris=redirect_uris,
            allowed_scopes=allowed_scopes,
        )
        self.session.add(app)
        await self.session.flush()
        return app, client_secret

    async def list_by_org(self, org_id: str) -> list[Application]:
        result = await self.session.execute(
            select(Application).where(Application.org_id == org_id).order_by(Application.created_at.desc())
        )
        return list(result.scalars().all())

    async def get(self, app_id: str, org_id: str) -> Application:
        result = await self.session.execute(
            select(Application).where(Application.id == app_id, Application.org_id == org_id)
        )
        app = result.scalar_one_or_none()
        if not app:
            raise NotFoundError("Application not found", code="app_not_found")
        return app

    async def update(self, app_id: str, org_id: str, **kwargs) -> Application:
        app = await self.get(app_id, org_id)
        for key, value in kwargs.items():
            if value is not None:
                setattr(app, key, value)
        return app

    async def rotate_secret(self, app_id: str, org_id: str) -> tuple[Application, str]:
        app = await self.get(app_id, org_id)
        new_secret = self._generate_client_secret()
        app.secret_hash = hash_token(new_secret)
        return app, new_secret

    async def delete(self, app_id: str, org_id: str) -> None:
        app = await self.get(app_id, org_id)
        await self.session.delete(app)

    async def authenticate_client(self, client_id: str, client_secret: str) -> Application:
        result = await self.session.execute(
            select(Application).where(Application.client_id == client_id)
        )
        app = result.scalar_one_or_none()
        if not app or not app.is_active:
            raise AuthError("Invalid client credentials", code="invalid_client")
        if not verify_token(client_secret, app.secret_hash):
            raise AuthError("Invalid client credentials", code="invalid_client")
        return app
