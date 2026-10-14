from __future__ import annotations

import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialWithJitterBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError


def create_redis(url: str) -> aioredis.Redis:
    return aioredis.from_url(
        url,
        decode_responses=False,
        health_check_interval=30,
        socket_keepalive=True,
        socket_connect_timeout=2,
        socket_timeout=5,
        retry=Retry(ExponentialWithJitterBackoff(base=0.05, cap=1.0), retries=3),
        retry_on_error=[RedisConnectionError, RedisTimeoutError],
    )


GCRA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local interval = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local tat = tonumber(redis.call('GET', KEYS[1]) or now)
if tat < now then tat = now end
local new_tat = tat + interval * cost
local allow_at = new_tat - burst
if allow_at > now then
  return {0, math.ceil(allow_at - now), 0}
end
redis.call('SET', KEYS[1], new_tat, 'PX', math.ceil(new_tat - now) + 1)
local remaining = math.floor((burst - (new_tat - now)) / interval)
return {1, 0, remaining}
"""

LEASE_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

LEASE_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
