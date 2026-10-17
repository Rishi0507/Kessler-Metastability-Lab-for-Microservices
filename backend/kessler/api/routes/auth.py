from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, field_validator

from ...errors import ApiError
from ...security import (
    DUMMY_HASH,
    hash_password,
    issue_access_token,
    new_csrf_token,
    new_refresh_token,
    sha256,
    verify_password,
)
from ..common import JSON, log_activity
from ..context import AppContext
from ..deps import ACCESS_COOKIE, CSRF_COOKIE, REFRESH_COOKIE, Ctx, MaybeUser, User, client_ip, limit

router = APIRouter(prefix="/api/v1", tags=["auth"])

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,63}$")
HANDLE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,31}$")


class RegisterIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=10, max_length=200)
    handle: str = Field(min_length=3, max_length=32)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip()
        if not EMAIL.match(v):
            raise ValueError("enter a valid email address")
        return v

    @field_validator("handle")
    @classmethod
    def _handle(cls, v: str) -> str:
        v = v.strip().lower()
        if not HANDLE.match(v) or v.startswith("guest-"):
            raise ValueError("3-32 characters: lowercase letters, digits, '-' or '_'")
        return v


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


def _user_json(r: asyncpg.Record) -> dict:
    return {
        "id": str(r["id"]),
        "handle": r["handle"],
        "email": r["email"],
        "is_guest": r["is_guest"],
        "created_at": r["created_at"],
    }


async def _start_session(
    ctx: AppContext, conn: asyncpg.Connection, user: asyncpg.Record, family: uuid.UUID | None = None
) -> tuple[str, str, str]:
    s = ctx.settings
    refresh, refresh_hash = new_refresh_token()
    await conn.execute(
        """INSERT INTO refresh_tokens (user_id, family_id, token_hash, expires_at)
           VALUES ($1, $2, $3, $4)""",
        user["id"],
        family or uuid.uuid4(),
        refresh_hash,
        datetime.now(UTC) + timedelta(seconds=s.refresh_ttl_s),
    )
    access = issue_access_token(s, str(user["id"]), user["handle"], user["is_guest"])
    return access, refresh, new_csrf_token()


def _session_response(ctx: AppContext, body: dict, cookies: tuple[str, str, str], status: int = 200) -> Response:
    s = ctx.settings
    access, refresh, csrf = cookies
    resp = JSON(body, status_code=status)
    common = {"secure": s.cookie_secure, "samesite": "strict"}
    resp.set_cookie(ACCESS_COOKIE, access, max_age=s.access_ttl_s, httponly=True, path="/", **common)
    resp.set_cookie(REFRESH_COOKIE, refresh, max_age=s.refresh_ttl_s, httponly=True, path="/api/v1/auth", **common)
    resp.set_cookie(CSRF_COOKIE, csrf, max_age=s.refresh_ttl_s, httponly=False, path="/", **common)
    return resp


def _clear_session(resp: Response) -> None:
    resp.delete_cookie(ACCESS_COOKIE, path="/")
    resp.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")
    resp.delete_cookie(CSRF_COOKIE, path="/")


@router.post("/auth/guest", status_code=201)
async def guest(request: Request, ctx: Ctx) -> Response:
    await limit(ctx, "auth", client_ip(request, ctx))
    async with ctx.db.write() as conn:
        for _ in range(5):
            handle = "guest-" + secrets.token_hex(3)
            user = await conn.fetchrow(
                """INSERT INTO users (handle, is_guest) VALUES ($1, true)
                   ON CONFLICT (handle) DO NOTHING RETURNING *""",
                handle,
            )
            if user is not None:
                break
        else:
            raise ApiError(503, "try_again", "Could not allocate a guest account, try again")
        cookies = await _start_session(ctx, conn, user)
        await log_activity(conn, str(user["id"]), "user.guest")
    return _session_response(ctx, {"user": _user_json(user)}, cookies, 201)


@router.post("/auth/register", status_code=201)
async def register(body: RegisterIn, request: Request, ctx: Ctx, principal: MaybeUser) -> Response:
    await limit(ctx, "auth", client_ip(request, ctx))
    pw_hash = await hash_password(body.password)
    async with ctx.db.write() as conn:
        try:
            if principal is not None and principal.guest:
                user = await conn.fetchrow(
                    """UPDATE users SET email = $2, password_hash = $3, handle = $4, is_guest = false
                       WHERE id = $1 AND is_guest RETURNING *""",
                    uuid.UUID(principal.user_id),
                    body.email,
                    pw_hash,
                    body.handle,
                )
                kind = "user.upgraded"
            else:
                user = await conn.fetchrow(
                    "INSERT INTO users (email, handle, password_hash) VALUES ($1, $2, $3) RETURNING *",
                    body.email,
                    body.handle,
                    pw_hash,
                )
                kind = "user.registered"
        except asyncpg.UniqueViolationError as exc:
            field = "email" if "email" in (exc.constraint_name or "") else "handle"
            raise ApiError(409, f"{field}_taken", f"That {field} is already taken") from None
        if user is None:
            raise ApiError(409, "not_a_guest", "This account is already registered")
        cookies = await _start_session(ctx, conn, user)
        await log_activity(conn, str(user["id"]), kind)
    return _session_response(ctx, {"user": _user_json(user)}, cookies, 201)


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, ctx: Ctx) -> Response:
    await limit(ctx, "auth", client_ip(request, ctx))
    async with ctx.db.primary_conn() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE email = $1 AND NOT is_guest", body.email.strip())
    ok = await verify_password(user["password_hash"] if user else DUMMY_HASH, body.password)
    if user is None or not ok:
        raise ApiError(401, "bad_credentials", "Email or password is incorrect")
    async with ctx.db.write() as conn:
        cookies = await _start_session(ctx, conn, user)
        await conn.execute("UPDATE users SET last_seen_at = now() WHERE id = $1", user["id"])
        await log_activity(conn, str(user["id"]), "user.login")
    return _session_response(ctx, {"user": _user_json(user)}, cookies, 200)


@router.post("/auth/refresh")
async def refresh(request: Request, ctx: Ctx) -> Response:
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise ApiError(401, "no_session", "No session to refresh")
    async with ctx.db.write() as conn:
        rt = await conn.fetchrow("SELECT * FROM refresh_tokens WHERE token_hash = $1 FOR UPDATE", sha256(token))
        if rt is None or rt["revoked_at"] is not None or rt["expires_at"] < datetime.now(UTC):
            raise ApiError(401, "session_expired", "Your session has expired, please sign in again")
        if rt["used_at"] is not None:
            await conn.execute(
                "UPDATE refresh_tokens SET revoked_at = now() WHERE family_id = $1 AND revoked_at IS NULL",
                rt["family_id"],
            )
            await log_activity(conn, str(rt["user_id"]), "security.refresh_reuse", {"family": str(rt["family_id"])})
            raise ApiError(401, "session_revoked", "This session was revoked, please sign in again")
        await conn.execute("UPDATE refresh_tokens SET used_at = now() WHERE id = $1", rt["id"])
        user = await conn.fetchrow("UPDATE users SET last_seen_at = now() WHERE id = $1 RETURNING *", rt["user_id"])
        if user is None:
            raise ApiError(401, "session_expired", "Your session has expired, please sign in again")
        cookies = await _start_session(ctx, conn, user, family=rt["family_id"])
    return _session_response(ctx, {"user": _user_json(user)}, cookies, 200)


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, ctx: Ctx) -> Response:
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        async with ctx.db.write() as conn:
            fam = await conn.fetchval("SELECT family_id FROM refresh_tokens WHERE token_hash = $1", sha256(token))
            if fam:
                await conn.execute(
                    "UPDATE refresh_tokens SET revoked_at = now() WHERE family_id = $1 AND revoked_at IS NULL", fam
                )
    resp = Response(status_code=204)
    _clear_session(resp)
    return resp


@router.get("/me")
async def me(user: User, ctx: Ctx) -> dict:
    async with ctx.db.read() as conn:
        r = await conn.fetchrow("SELECT * FROM users WHERE id = $1", uuid.UUID(user.user_id))
    if r is None:
        raise ApiError(401, "unauthenticated", "Sign in again")
    return {"user": _user_json(r), "auth": user.via}
