from __future__ import annotations

from dataclasses import dataclass

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from . import keys
from .logs import get_logger
from .metrics import RATE_LIMITED
from .redisio import GCRA

log = get_logger("kessler.ratelimit")


@dataclass(frozen=True)
class Limit:
    per_minute: int
    burst: int

    @property
    def interval_ms(self) -> float:
        return 60_000.0 / self.per_minute


LIMITS: dict[str, Limit] = {
    "auth": Limit(per_minute=12, burst=6),
    "write": Limit(per_minute=120, burst=30),
    "submit": Limit(per_minute=20, burst=5),
    "control": Limit(per_minute=600, burst=60),
    "read": Limit(per_minute=1200, burst=200),
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_s: float
    remaining: int
    limit: Limit


class RateLimiter:
    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        self._script = redis.register_script(GCRA)

    async def check(self, bucket: str, subject: str, cost: int = 1) -> Decision:
        limit = LIMITS[bucket]
        try:
            allowed, retry_ms, remaining = await self._script(
                keys=[keys.ratelimit(bucket, subject)],
                args=[limit.interval_ms, limit.burst * limit.interval_ms, cost],
            )
        except RedisError as exc:
            log.warning("rate limiter unavailable, allowing request", extra={"bucket": bucket, "error": str(exc)})
            return Decision(True, 0.0, limit.burst, limit)
        if not allowed:
            RATE_LIMITED.labels(bucket).inc()
        return Decision(bool(allowed), int(retry_ms) / 1000.0, int(remaining), limit)
