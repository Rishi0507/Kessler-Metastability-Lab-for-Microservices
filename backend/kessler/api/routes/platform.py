from __future__ import annotations

import asyncio
import secrets
import time
import uuid
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from ... import keys
from ...errors import ApiError
from ...metrics import render
from ...security import new_api_key
from ..common import JSON
from ..context import AppContext
from ..deps import Ctx, User, limit, parse_uuid

router = APIRouter(tags=["platform"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(ctx: Ctx) -> Response:
    db_ok = await ctx.db.healthy()
    try:
        redis_ok = bool(await asyncio.wait_for(ctx.redis.ping(), timeout=1.0))
    except Exception:
        redis_ok = False
    ok = db_ok and redis_ok
    return JSON(
        {"status": "ready" if ok else "degraded", "db": db_ok, "redis": redis_ok}, status_code=200 if ok else 503
    )


@router.get("/metrics")
async def metrics() -> Response:
    body, ctype = render()
    return Response(body, media_type=ctype)


@router.post("/api/v1/realtime/tickets", status_code=201)
async def ticket(user: User, ctx: Ctx) -> dict[str, Any]:
    await limit(ctx, "write", user.user_id)
    token = secrets.token_urlsafe(24)
    await ctx.redis.set(keys.ticket(token), user.user_id.encode(), ex=30)
    return {"ticket": token, "expires_in": 30}


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@router.post("/api/v1/api-keys", status_code=201)
async def create_key(body: KeyIn, user: User, ctx: Ctx) -> dict[str, Any]:
    if user.guest:
        raise ApiError(403, "guests_cannot_create_keys", "Create an account to use API keys")
    full, prefix, key_hash = new_api_key()
    async with ctx.db.write() as conn:
        row = await conn.fetchrow(
            "INSERT INTO api_keys (user_id, name, prefix, key_hash) VALUES ($1, $2, $3, $4) RETURNING id, created_at",
            uuid.UUID(user.user_id),
            body.name,
            prefix,
            key_hash,
        )
    return {
        "id": str(row["id"]),
        "name": body.name,
        "key": full,
        "prefix": prefix,
        "created_at": row["created_at"],
        "note": "Copy this key now; it is not shown again.",
    }


@router.get("/api/v1/api-keys")
async def list_keys(user: User, ctx: Ctx) -> dict[str, Any]:
    async with ctx.db.read() as conn:
        rows = await conn.fetch(
            """SELECT id, name, prefix, created_at, last_used_at FROM api_keys
               WHERE user_id = $1 AND revoked_at IS NULL ORDER BY created_at DESC""",
            uuid.UUID(user.user_id),
        )
    return {"items": [dict(r) | {"id": str(r["id"])} for r in rows]}


@router.delete("/api/v1/api-keys/{key_id}", status_code=204)
async def revoke_key(key_id: str, user: User, ctx: Ctx) -> Response:
    async with ctx.db.write() as conn:
        ok = await conn.fetchval(
            "UPDATE api_keys SET revoked_at = now() WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL RETURNING 1",
            parse_uuid(key_id, "key"),
            uuid.UUID(user.user_id),
        )
    if not ok:
        raise ApiError(404, "not_found", "No such key")
    return Response(status_code=204)


_kpi_cache: tuple[float, dict[str, Any] | None] = (0.0, None)
_kpi_lock = asyncio.Lock()

KPI_SQL = """
WITH recent AS (SELECT * FROM analyses WHERE created_at > now() - interval '24 hours')
SELECT
  (SELECT count(*) FROM recent) AS analyses_24h,
  (SELECT count(*) FROM recent WHERE status = 'done') AS analyses_done_24h,
  (SELECT count(*) FROM analyses WHERE status IN ('queued', 'running')) AS analyses_active,
  (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM first_result_at - created_at))
     FROM recent WHERE first_result_at IS NOT NULL) AS ttfi_p50,
  (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM first_result_at - created_at))
     FROM recent WHERE first_result_at IS NOT NULL) AS ttfi_p95,
  (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - created_at))
     FROM recent WHERE status = 'done') AS completion_p50,
  (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - created_at))
     FROM recent WHERE status = 'done') AS completion_p95,
  (SELECT coalesce(sum(sims_done), 0) FROM recent) AS sims_24h,
  (SELECT coalesce(sum(sims_cached), 0) FROM recent) AS sims_cached_24h,
  (SELECT avg((result->'savings'->>'avoided_fraction')::float) FROM recent WHERE status = 'done') AS avoided_avg,
  (SELECT count(*) FROM runs WHERE status = 'running') AS live_runs,
  (SELECT count(DISTINCT owner_id) FROM recent) AS active_users_24h,
  (SELECT count(*) FROM analyses WHERE parent_id IS NOT NULL
     AND created_at > now() - interval '7 days') AS experiments_7d,
  (SELECT count(*) FROM analyses WHERE status = 'done' AND parent_id IS NULL
     AND created_at > now() - interval '7 days') AS analyses_done_7d,
  (SELECT avg((c.result->>'safe_fraction')::float - (p.result->>'safe_fraction')::float)
     FROM analyses c JOIN analyses p ON p.id = c.parent_id
     WHERE c.status = 'done' AND p.status = 'done' AND c.created_at > now() - interval '30 days') AS fix_gain_avg
"""


async def _service_stats(ctx: AppContext) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    names = [k async for k in ctx.redis.scan_iter(match=keys.SERVICE_STATS_PATTERN, count=200)]
    if not names:
        return out
    pipe = ctx.redis.pipeline(transaction=False)
    for n in names:
        pipe.hgetall(n)
    for h in await pipe.execute():
        if not h:
            continue
        d = {k.decode(): v.decode() for k, v in h.items()}
        out.setdefault(d.get("kind", "unknown"), []).append(d)
    return out


async def _queues(ctx: AppContext) -> dict[str, Any]:
    q: dict[str, Any] = {"backlog": 0, "inflight": 0, "results_lag": 0}
    try:
        tenants = await ctx.redis.zrange(keys.TENANTS, 0, -1)
        if tenants:
            pipe = ctx.redis.pipeline(transaction=False)
            for t in tenants:
                pipe.llen(keys.tenant_queue(t.decode()))
            q["backlog"] = int(sum(await pipe.execute()))
        q["tenants_waiting"] = len(tenants)
        for stream, group, field in (
            (keys.STREAM_TASKS, keys.GROUP_WORKERS, "inflight"),
            (keys.STREAM_RESULTS, keys.GROUP_SCHEDULER, "results_lag"),
        ):
            try:
                for g in await ctx.redis.xinfo_groups(stream):
                    if g["name"].decode() == group:
                        q[field] = int(g.get("pending", 0) or 0) + int(g.get("lag", 0) or 0)
            except Exception:
                q[field] = 0
    except Exception:
        q["error"] = "redis unavailable"
    return q


def _num(v: Any, digits: int = 3) -> float | None:
    return round(float(v), digits) if v is not None else None


async def _compute_kpis(ctx: AppContext) -> dict[str, Any]:
    async with ctx.db.read() as conn:
        r = await conn.fetchrow(KPI_SQL)
    svc = await _service_stats(ctx)
    queues = await _queues(ctx)
    sims = int(r["sims_24h"] or 0)
    cached = int(r["sims_cached_24h"] or 0)
    workers = svc.get("worker", [])
    gateways = svc.get("gateway", [])
    return {
        "generated_at": time.time(),
        "slo": {
            "time_to_first_insight_p50_s": _num(r["ttfi_p50"]),
            "time_to_first_insight_p95_s": _num(r["ttfi_p95"]),
            "analysis_completion_p50_s": _num(r["completion_p50"]),
            "analysis_completion_p95_s": _num(r["completion_p95"]),
        },
        "throughput": {
            "analyses_24h": r["analyses_24h"],
            "analyses_done_24h": r["analyses_done_24h"],
            "analyses_active": r["analyses_active"],
            "simulations_24h": sims,
            "cache_hit_ratio": round(cached / sims, 4) if sims else None,
            "adaptive_sims_avoided": _num(r["avoided_avg"], 4),
            "engine_events_per_s": round(sum(float(w.get("events_per_s", 0) or 0) for w in workers)),
        },
        "realtime": {
            "live_runs": r["live_runs"],
            "ws_connections": sum(int(g.get("connections", 0) or 0) for g in gateways),
            "fanout_p99_ms": max((float(g.get("fanout_p99_ms", 0) or 0) for g in gateways), default=None),
        },
        "capacity": {
            "workers": len(workers),
            "worker_slots": sum(int(w.get("slots", 0) or 0) for w in workers),
            "worker_busy": sum(int(w.get("busy", 0) or 0) for w in workers),
            "gateways": len(gateways),
            "api_replicas": len(svc.get("api", [])),
            "scheduler_leader": next(
                (s.get("instance") for s in svc.get("scheduler", []) if s.get("leader") == "1"), None
            ),
            **queues,
        },
        "product": {
            "active_users_24h": r["active_users_24h"],
            "fix_adoption_7d": round(r["experiments_7d"] / r["analyses_done_7d"], 4) if r["analyses_done_7d"] else None,
            "stable_region_gain_avg": _num(r["fix_gain_avg"], 4),
        },
        "instances": svc,
    }


@router.get("/api/v1/platform/kpis")
async def kpis(ctx: Ctx) -> Response:
    global _kpi_cache
    ts, cached = _kpi_cache
    if cached is None or time.monotonic() - ts > 2.0:
        async with _kpi_lock:
            ts, cached = _kpi_cache
            if cached is None or time.monotonic() - ts > 2.0:
                cached = await _compute_kpis(ctx)
                _kpi_cache = (time.monotonic(), cached)
    return JSON(cached, headers={"cache-control": "public, max-age=2"})
