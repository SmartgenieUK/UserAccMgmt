from __future__ import annotations

import os
import asyncio
import sys
import types
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

try:
    import aiosmtplib  # type: ignore
except ModuleNotFoundError:
    aiosmtplib = types.ModuleType("aiosmtplib")

    async def _fake_send(*args, **kwargs):
        return {}

    aiosmtplib.send = _fake_send  # type: ignore[attr-defined]
    sys.modules["aiosmtplib"] = aiosmtplib

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test.db"
os.environ["REDIS_REQUIRED"] = "false"
os.environ["SMTP_HOST"] = "localhost"
os.environ["SMTP_PORT"] = "25"
os.environ["SMTP_USER"] = "test"
os.environ["SMTP_PASSWORD"] = "test"
os.environ["EMAIL_FROM"] = "noreply@example.com"
os.environ["SECRET_KEY"] = "test_secret_key_32_chars_minimum"
os.environ["PUBLIC_BASE_URL"] = "http://localhost"

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import app

get_settings.cache_clear()


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.get_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def engine():
    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture()
async def db_session(engine):
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        yield session


@pytest.fixture()
async def client(db_session):
    async def override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = override_get_session
    async with AsyncClient(app=app, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def mock_email_delivery(monkeypatch):
    async def _fake_send(*args, **kwargs):
        return {}

    monkeypatch.setattr(aiosmtplib, "send", _fake_send, raising=False)
