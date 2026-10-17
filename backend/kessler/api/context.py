from __future__ import annotations

from dataclasses import dataclass

import redis.asyncio as aioredis

from ..config import Settings
from ..db import Database
from ..ratelimit import RateLimiter


@dataclass
class AppContext:
    settings: Settings
    db: Database
    redis: aioredis.Redis
    limiter: RateLimiter
