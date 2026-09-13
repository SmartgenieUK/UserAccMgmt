from __future__ import annotations

import os
import sys
import types
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
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


class FakeRedis:
    """The subset of redis.asyncio the app uses: OTP storage and rate-limit counters."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    async def expire(self, key, ttl):
        return True


@pytest.fixture()
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
def fake_redis():
    return FakeRedis()


@pytest.fixture()
async def client(db_session, fake_redis):
    async def override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = override_get_session
    app.state.redis = fake_redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    app.state.redis = None


@pytest.fixture(autouse=True)
def mock_email_delivery(monkeypatch):
    async def _fake_send(*args, **kwargs):
        return {}

    monkeypatch.setattr(aiosmtplib, "send", _fake_send, raising=False)


@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[bytes, bytes]:
    """A throwaway RS256 keypair, generated per test session and never written to disk.

    Tests that mint or verify asymmetric tokens use this instead of the shared SECRET_KEY,
    so the test base cannot forge production-shaped tokens.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem
