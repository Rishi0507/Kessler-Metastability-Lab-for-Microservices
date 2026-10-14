from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path

import asyncpg

from ..config import get_settings

VERSIONS = Path(__file__).parent / "versions"
LOCK_KEY = 7_291_337


async def migrate(dsn: str) -> list[str]:
    conn = await asyncpg.connect(dsn)
    applied_now: list[str] = []
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_KEY)
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version    text PRIMARY KEY,
                   checksum   text NOT NULL,
                   applied_at timestamptz NOT NULL DEFAULT now())"""
        )
        applied = {
            r["version"]: r["checksum"] for r in await conn.fetch("SELECT version, checksum FROM schema_migrations")
        }
        for path in sorted(VERSIONS.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            version = path.stem
            if version in applied:
                if applied[version] != checksum:
                    raise SystemExit(f"migration {version} changed after it was applied (checksum mismatch)")
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES ($1, $2)", version, checksum
                )
            applied_now.append(version)
        await conn.execute("SELECT ensure_activity_partitions()")
    finally:
        try:
            await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)
        finally:
            await conn.close()
    return applied_now


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply Kessler database migrations")
    parser.add_argument("--dsn", default=None, help="Postgres DSN (defaults to KESSLER_DATABASE_URL)")
    args = parser.parse_args()
    dsn = args.dsn or get_settings().database_url
    done = asyncio.run(migrate(dsn))
    print(f"applied {len(done)} migration(s): {', '.join(done) or 'none pending'}", file=sys.stderr)


if __name__ == "__main__":
    main()
