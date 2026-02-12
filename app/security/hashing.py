from __future__ import annotations

from passlib.context import CryptContext

_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _pwd_context.verify(password, password_hash)


def hash_token(token: str) -> str:
    return _pwd_context.hash(token)


def verify_token(token: str, token_hash: str) -> bool:
    return _pwd_context.verify(token, token_hash)
