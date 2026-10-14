from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .config import Settings

_hasher = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(stored: str, password: str) -> bool:
    def _verify() -> bool:
        try:
            return _hasher.verify(stored, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    return await asyncio.to_thread(_verify)


DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sha256(data: str) -> bytes:
    return hashlib.sha256(data.encode()).digest()


@dataclass(frozen=True)
class Principal:
    user_id: str
    handle: str
    guest: bool
    via: str = "cookie"


def issue_access_token(s: Settings, user_id: str, handle: str, guest: bool) -> str:
    now = int(time.time())
    claims = {
        "sub": user_id,
        "handle": handle,
        "guest": guest,
        "iat": now,
        "exp": now + s.access_ttl_s,
        "jti": uuid.uuid4().hex,
        "typ": "access",
    }
    return jwt.encode(claims, s.jwt_secret.get_secret_value(), algorithm="HS256")


def decode_access_token(s: Settings, token: str) -> Principal | None:
    try:
        c = jwt.decode(
            token,
            s.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            options={"require": ["exp", "iat", "sub"]},
            leeway=5,
        )
    except jwt.PyJWTError:
        return None
    if c.get("typ") != "access":
        return None
    return Principal(user_id=str(c["sub"]), handle=str(c.get("handle", "")), guest=bool(c.get("guest", False)))


def new_refresh_token() -> tuple[str, bytes]:
    token = secrets.token_urlsafe(32)
    return token, sha256(token)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def sign_stream_token(s: Settings, topic: str, user_id: str, perms: str = "r", ttl_s: int = 3600) -> str:
    exp = int(time.time()) + ttl_s
    payload = f"v1|{topic}|{user_id}|{perms}|{exp}"
    sig = hmac.new(s.stream_secret.get_secret_value().encode(), payload.encode(), hashlib.sha256).digest()
    return b64url(payload.encode()) + "." + b64url(sig)


def verify_stream_token(s: Settings, token: str) -> dict[str, Any] | None:
    try:
        body, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
        expected = hmac.new(s.stream_secret.get_secret_value().encode(), payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(b64url(expected), sig):
            return None
        version, topic, user_id, perms, exp = payload.split("|")
        if version != "v1" or int(exp) < time.time():
            return None
        return {"topic": topic, "user_id": user_id, "perms": perms, "exp": int(exp)}
    except (ValueError, UnicodeDecodeError):
        return None


def new_api_key() -> tuple[str, str, bytes]:
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    return f"ks_{prefix}_{secret}", prefix, sha256(secret)


def split_api_key(key: str) -> tuple[str, str] | None:
    parts = key.split("_", 2)
    if len(parts) != 3 or parts[0] != "ks" or len(parts[1]) != 8:
        return None
    return parts[1], parts[2]


def api_key_matches(stored_hash: bytes, secret: str) -> bool:
    return hmac.compare_digest(bytes(stored_hash), sha256(secret))
