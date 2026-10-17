from __future__ import annotations

import time
import uuid
from typing import Annotated

from fastapi import Depends, Header, Request

from ..errors import ApiError
from ..logs import user_id_var
from ..security import Principal, api_key_matches, decode_access_token, split_api_key
from .context import AppContext

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ACCESS_COOKIE = "ks_access"
REFRESH_COOKIE = "ks_refresh"
CSRF_COOKIE = "ks_csrf"
CSRF_HEADER = "x-csrf-token"

_key_cache: dict[str, tuple[str, str, bytes, float]] = {}


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


async def _principal_from_api_key(ctx: AppContext, key: str) -> Principal | None:
    parts = split_api_key(key)
    if not parts:
        return None
    prefix, secret = parts
    cached = _key_cache.get(prefix)
    if cached is None or cached[3] < time.monotonic():
        async with ctx.db.read() as conn:
            row = await conn.fetchrow(
                """SELECT k.user_id, k.key_hash, u.handle FROM api_keys k JOIN users u ON u.id = k.user_id
                   WHERE k.prefix = $1 AND k.revoked_at IS NULL""",
                prefix,
            )
        if row is None:
            return None
        cached = (str(row["user_id"]), row["handle"], bytes(row["key_hash"]), time.monotonic() + 60)
        _key_cache[prefix] = cached
    user_id, handle, key_hash, _ = cached
    if not api_key_matches(key_hash, secret):
        return None
    return Principal(user_id=user_id, handle=handle, guest=False, via="api_key")


async def optional_principal(request: Request, ctx: Ctx) -> Principal | None:
    auth = request.headers.get("authorization", "")
    principal: Principal | None = None
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token.startswith("ks_"):
            principal = await _principal_from_api_key(ctx, token)
        else:
            p = decode_access_token(ctx.settings, token)
            principal = Principal(p.user_id, p.handle, p.guest, via="bearer") if p else None
        if principal is None:
            raise ApiError(401, "invalid_token", "The bearer token is invalid or expired")
    else:
        token = request.cookies.get(ACCESS_COOKIE)
        principal = decode_access_token(ctx.settings, token) if token else None
        if principal is not None and request.method not in SAFE_METHODS:
            sent = request.headers.get(CSRF_HEADER)
            cookie = request.cookies.get(CSRF_COOKIE)
            if not sent or not cookie or sent != cookie:
                raise ApiError(403, "csrf_failed", "Missing or invalid CSRF token")
    if principal is not None:
        user_id_var.set(principal.user_id)
    return principal


async def require_principal(principal: Annotated[Principal | None, Depends(optional_principal)]) -> Principal:
    if principal is None:
        raise ApiError(401, "unauthenticated", "Sign in or continue as a guest first")
    return principal


User = Annotated[Principal, Depends(require_principal)]
MaybeUser = Annotated[Principal | None, Depends(optional_principal)]


def client_ip(request: Request, ctx: AppContext) -> str:
    peer = request.client.host if request.client else "unknown"
    trusted = {p.strip() for p in ctx.settings.trusted_proxies.split(",")}
    if peer in trusted:
        xff = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        for hop in reversed(hops):
            if hop not in trusted:
                return hop
    return peer


async def limit(ctx: AppContext, bucket: str, subject: str, cost: int = 1) -> None:
    d = await ctx.limiter.check(bucket, subject, cost)
    if not d.allowed:
        retry = max(1, round(d.retry_after_s))
        raise ApiError(
            429,
            "rate_limited",
            "Too many requests",
            f"Try again in {retry} s.",
            headers={"Retry-After": str(retry), "X-RateLimit-Limit": str(d.limit.per_minute)},
        )


def parse_uuid(value: str, what: str = "resource") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ApiError(404, "not_found", f"No such {what}") from None


MinLsn = Annotated[str | None, Header(alias="x-kessler-min-lsn")]
IdempotencyKey = Annotated[str | None, Header(alias="idempotency-key")]
IfMatch = Annotated[str | None, Header(alias="if-match")]
IfNoneMatch = Annotated[str | None, Header(alias="if-none-match")]
