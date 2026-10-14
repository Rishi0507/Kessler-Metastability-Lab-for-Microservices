from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import orjson

from .config import Settings
from .logs import get_logger
from .metrics import DB_READS, REPLICA_LAG

log = get_logger("kessler.db")


def parse_lsn(lsn: str | None) -> int | None:
    if not lsn:
        return None
    try:
        hi, lo = lsn.split("/")
        return (int(hi, 16) << 32) | int(lo, 16)
    except (ValueError, AttributeError):
        return None


async def _init_connection(conn: asyncpg.Connection) -> None:
    for typ in ("jsonb", "json"):
        await conn.set_type_codec(
            typ,
            encoder=lambda v: orjson.dumps(v).decode(),
            decoder=orjson.loads,
            schema="pg_catalog",
            format="text",
        )


class Database:
    def __init__(self, settings: Settings, app_name: str) -> None:
        self._s = settings
        self._app = app_name
        self.primary: asyncpg.Pool | None = None
        self.replica: asyncpg.Pool | None = None
        self._replay_lsn: int | None = None
        self._replay_at = 0.0
        self._replay_lock = asyncio.Lock()

    async def _pool(self, dsn: str) -> asyncpg.Pool:
        return await asyncpg.create_pool(
            dsn,
            min_size=self._s.db_pool_min,
            max_size=self._s.db_pool_max,
            statement_cache_size=self._s.db_statement_cache_size,
            command_timeout=self._s.db_command_timeout_s,
            max_inactive_connection_lifetime=60,
            init=_init_connection,
            server_settings={"application_name": self._app},
        )

    async def connect(self) -> None:
        self.primary = await self._pool(self._s.database_url)
        if self._s.database_replica_url:
            try:
                self.replica = await self._pool(self._s.database_replica_url)
            except (OSError, asyncpg.PostgresError) as exc:
                log.warning("replica unavailable, reading from primary", extra={"error": str(exc)})
                self.replica = None

    async def close(self) -> None:
        for pool in (self.primary, self.replica):
            if pool is not None:
                await pool.close()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[asyncpg.Connection]:
        assert self.primary is not None
        async with self.primary.acquire() as conn, conn.transaction():
            yield conn

    @asynccontextmanager
    async def primary_conn(self) -> AsyncIterator[asyncpg.Connection]:
        assert self.primary is not None
        async with self.primary.acquire() as conn:
            yield conn

    async def _replica_replay_lsn(self) -> int | None:
        if self.replica is None:
            return None
        now = time.monotonic()
        if now - self._replay_at < 0.1:
            return self._replay_lsn
        async with self._replay_lock:
            if time.monotonic() - self._replay_at < 0.1:
                return self._replay_lsn
            try:
                row = await self.replica.fetchrow("SELECT pg_last_wal_replay_lsn()::text AS lsn")
                self._replay_lsn = parse_lsn(row["lsn"]) if row else None
            except (OSError, asyncpg.PostgresError):
                self._replay_lsn = None
            self._replay_at = time.monotonic()
            return self._replay_lsn

    @asynccontextmanager
    async def read(self, min_lsn: str | None = None) -> AsyncIterator[asyncpg.Connection]:
        assert self.primary is not None
        target, reason = "primary", "no_replica"
        if self.replica is not None:
            want = parse_lsn(min_lsn)
            have = await self._replica_replay_lsn()
            if have is None:
                reason = "replica_unknown"
            elif want is None or have >= want:
                target, reason = "replica", "caught_up" if want else "no_lsn"
            else:
                reason = "replica_behind"
        DB_READS.labels(target, reason).inc()
        pool = self.replica if target == "replica" else self.primary
        assert pool is not None
        async with pool.acquire() as conn:
            yield conn

    async def current_lsn(self, conn: asyncpg.Connection) -> str:
        return await conn.fetchval("SELECT pg_current_wal_insert_lsn()::text")

    async def refresh_replica_lag(self) -> None:
        if self.replica is None or self.primary is None:
            return
        try:
            primary = parse_lsn(await self.primary.fetchval("SELECT pg_current_wal_lsn()::text"))
            replica = parse_lsn(await self.replica.fetchval("SELECT pg_last_wal_replay_lsn()::text"))
            if primary is not None and replica is not None:
                REPLICA_LAG.set(max(0, primary - replica))
        except (OSError, asyncpg.PostgresError):
            pass

    async def healthy(self) -> bool:
        try:
            assert self.primary is not None
            return await self.primary.fetchval("SELECT 1") == 1
        except Exception:
            return False


def record(row: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
