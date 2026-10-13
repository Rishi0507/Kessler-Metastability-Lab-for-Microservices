from __future__ import annotations

import time
from typing import Any

import orjson
import redis.asyncio as aioredis

from . import keys

STREAM_MAXLEN = 20_000


async def emit(
    redis: aioredis.Redis, topic: str, stream_key: str, event: dict[str, Any], *, ttl_s: int | None = None
) -> str:
    event = {**event, "ts": time.time()}
    data = orjson.dumps(event)
    sid = await redis.xadd(stream_key, {b"e": data}, maxlen=STREAM_MAXLEN, approximate=True)
    sid_s = sid.decode() if isinstance(sid, bytes) else str(sid)
    envelope = b'{"topic":' + orjson.dumps(topic) + b',"id":"' + sid_s.encode() + b'","event":' + data + b"}"
    pipe = redis.pipeline(transaction=False)
    pipe.publish(keys.channel(topic), envelope)
    if ttl_s:
        pipe.expire(stream_key, ttl_s)
    await pipe.execute()
    return sid_s


async def emit_analysis(
    redis: aioredis.Redis, analysis_id: str, event: dict[str, Any], ttl_s: int | None = None
) -> str:
    return await emit(redis, f"analysis:{analysis_id}", keys.progress_stream(analysis_id), event, ttl_s=ttl_s)


def compact_summary(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "verdict": s.get("verdict"),
        "recovery_s": s.get("recovery_s"),
        "peak_amplification": s.get("peak_amplification"),
        "wasted_fraction": s.get("wasted_fraction"),
        "min_goodput_ratio": s.get("min_goodput_ratio"),
        "lost_requests": s.get("lost_requests"),
        "events": s.get("events"),
        "wall_ms": s.get("wall_ms"),
        "truncated": s.get("truncated"),
    }
