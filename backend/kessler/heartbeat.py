from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Any

import redis.asyncio as aioredis

from . import keys
from .logs import get_logger

log = get_logger("kessler.heartbeat")


async def heartbeat_loop(
    redis: aioredis.Redis,
    kind: str,
    instance: str,
    stats: Callable[[], dict[str, Any]],
    interval_s: float = 5.0,
    ttl_s: int = 15,
) -> None:
    key = keys.service_stats(kind, instance)
    started = time.time()
    while True:
        try:
            data = {"kind": kind, "instance": instance, "started_at": started, "ts": time.time(), **stats()}
            await redis.hset(key, mapping={k: str(v) for k, v in data.items()})
            await redis.expire(key, ttl_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("heartbeat failed", extra={"error": str(exc)})
        await asyncio.sleep(interval_s)


async def supervise(fn: Any, name: str, logger: Any, max_delay_s: float = 10.0, first_delay_s: float = 0.5) -> None:
    delay = first_delay_s
    while True:
        try:
            await fn()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"{name} loop crashed; restarting in {delay:g} s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay_s)


async def cancel(*tasks: asyncio.Task[Any] | None) -> None:
    for t in tasks:
        if t is not None:
            t.cancel()
    for t in tasks:
        if t is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
