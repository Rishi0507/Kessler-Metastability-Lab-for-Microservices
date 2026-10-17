from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import asyncpg
import orjson
from starlette.responses import Response

from ..errors import ApiError
from .context import AppContext


class JSON(Response):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return orjson.dumps(content, option=orjson.OPT_NON_STR_KEYS)


def row(r: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def body_hash(payload: Any) -> str:
    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()


Handler = Callable[[asyncpg.Connection], Awaitable[tuple[int, dict[str, Any]]]]


async def idempotent(
    ctx: AppContext, user_id: str, key: str | None, request_hash: str, handler: Handler
) -> tuple[int, dict[str, Any], bool, str]:
    if key is not None and not (8 <= len(key) <= 128):
        raise ApiError(400, "bad_idempotency_key", "Idempotency-Key must be 8-128 characters")
    async with ctx.db.write() as conn:
        if key is not None:
            inserted = await conn.fetchval(
                """INSERT INTO idempotency_keys (user_id, key, request_hash) VALUES ($1, $2, $3)
                   ON CONFLICT (user_id, key) DO NOTHING RETURNING 1""",
                user_id,
                key,
                request_hash,
            )
            if not inserted:
                prev = await conn.fetchrow(
                    "SELECT request_hash, status_code, response FROM idempotency_keys WHERE user_id = $1 AND key = $2",
                    user_id,
                    key,
                )
                if prev is None or prev["status_code"] is None:
                    raise ApiError(409, "request_in_progress", "An identical request is still being processed")
                if prev["request_hash"] != request_hash:
                    raise ApiError(
                        422, "idempotency_key_reused", "This Idempotency-Key was used for a different request"
                    )
                return int(prev["status_code"]), prev["response"], True, ""
        status, body = await handler(conn)
        if key is not None:
            await conn.execute(
                "UPDATE idempotency_keys SET status_code = $3, response = $4 WHERE user_id = $1 AND key = $2",
                user_id,
                key,
                status,
                orjson.loads(orjson.dumps(body)),
            )
        lsn = await ctx.db.current_lsn(conn)
    return status, body, False, lsn


def encode_cursor(ts: datetime, ident: Any) -> str:
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{ident}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        ts, ident = raw.split("|", 1)
        return datetime.fromisoformat(ts), ident
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "bad_cursor", "The pagination cursor is invalid") from None


def etag_for_version(version: int) -> str:
    return f'"v{version}"'


def parse_if_match(header: str | None) -> int | None:
    if not header:
        return None
    h = header.strip()
    if h.startswith("W/"):
        h = h[2:]
    h = h.strip('"')
    if not h.startswith("v"):
        return None
    try:
        return int(h[1:])
    except ValueError:
        return None


async def log_activity(
    conn: asyncpg.Connection, user_id: str | None, kind: str, data: dict[str, Any] | None = None
) -> None:
    await conn.execute("INSERT INTO activity_log (user_id, kind, data) VALUES ($1, $2, $3)", user_id, kind, data or {})
