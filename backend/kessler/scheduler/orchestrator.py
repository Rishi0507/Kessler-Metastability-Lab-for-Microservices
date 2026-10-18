from __future__ import annotations

import time
from typing import Any

import asyncpg
import orjson
import redis.asyncio as aioredis

from .. import keys
from ..analysis.findings import summarize
from ..analysis.planner import (
    SweepParams,
    aggregate_cell,
    boundary,
    cell_is_bad,
    infer,
    plan_next,
    savings,
)
from ..config import Settings
from ..events import compact_summary, emit_analysis
from ..logs import get_logger
from ..metrics import ANALYSES_FINISHED, ANALYSIS_DURATION, SIMS, SIMS_AVOIDED
from ..spec import task_hash

log = get_logger("kessler.scheduler")

PROGRESS_TTL_S = 7 * 86_400


def tenant_of(priority: int, owner_id: str) -> str:
    return f"{priority}:{owner_id}"


async def enqueue(redis: aioredis.Redis, tenant: str, priority: int, payloads: list[bytes]) -> None:
    if not payloads:
        return
    pipe = redis.pipeline(transaction=False)
    pipe.rpush(keys.tenant_queue(tenant), *payloads)
    pipe.zadd(keys.TENANTS, {tenant: priority}, nx=True)
    await pipe.execute()


async def cell_states(
    conn: asyncpg.Connection, analysis_id: Any, params: SweepParams
) -> tuple[dict[int, bool | None], dict[int, dict[str, Any]]]:
    rows = await conn.fetch(
        "SELECT cell, seed_index, status, summary FROM analysis_tasks WHERE analysis_id = $1", analysis_id
    )
    by_cell: dict[int, list[Any]] = {}
    for r in rows:
        by_cell.setdefault(r["cell"], []).append(r)
    evaluated: dict[int, bool | None] = {}
    aggs: dict[int, dict[str, Any]] = {}
    for cell, ts in by_cell.items():
        finished = [t for t in ts if t["status"] in ("done", "failed")]
        if len(finished) < params.seeds:
            evaluated[cell] = None
            continue
        ok = [t["summary"] for t in finished if t["status"] == "done" and t["summary"]]
        agg = aggregate_cell(ok) if ok else {"seeds": 0, "bad": False, "failed": True}
        agg["bad"] = cell_is_bad(ok)
        evaluated[cell] = agg["bad"]
        aggs[cell] = agg
    return evaluated, aggs


async def schedule_more(
    conn: asyncpg.Connection,
    redis: aioredis.Redis,
    a: asyncpg.Record,
    settings: Settings,
    engine_version: str,
) -> tuple[bool, list[dict[str, Any]]]:
    params = SweepParams.from_dict(a["params"])
    events: list[dict[str, Any]] = []
    tenant = tenant_of(int(a["priority"]), str(a["owner_id"]))
    for _ in range(params.n_cells + 1):
        evaluated, _ = await cell_states(conn, a["id"], params)
        nxt = plan_next(params, evaluated)
        if not nxt:
            return all(v is not None for v in evaluated.values()), events
        tasks = []
        for cell in nxt:
            for seed in range(params.seeds):
                scenario = params.scenario(cell, seed) | {"max_events": settings.max_events_per_sim}
                tasks.append((cell, seed, task_hash(a["spec_hash"], scenario, engine_version), scenario))
        await conn.executemany(
            """INSERT INTO analysis_tasks (analysis_id, cell, seed_index, task_hash)
               VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING""",
            [(a["id"], c, s, h) for c, s, h, _ in tasks],
        )
        await conn.execute("UPDATE analyses SET sims_total = sims_total + $2 WHERE id = $1", a["id"], len(tasks))
        cached_rows = await conn.fetch(
            "SELECT task_hash, summary FROM sim_results WHERE task_hash = ANY($1::text[])", [h for _, _, h, _ in tasks]
        )
        cached = {r["task_hash"]: r["summary"] for r in cached_rows}
        events.append({"type": "planned", "cells": nxt})
        payloads: list[bytes] = []
        hits = 0
        for cell, seed, h, scenario in tasks:
            if h in cached:
                done = await conn.fetchval(
                    """UPDATE analysis_tasks SET status = 'done', cached = true, summary = $4, finished_at = now()
                       WHERE analysis_id = $1 AND cell = $2 AND seed_index = $3 AND status <> 'done' RETURNING 1""",
                    a["id"],
                    cell,
                    seed,
                    cached[h],
                )
                if done:
                    hits += 1
                    events.append(
                        {
                            "type": "task",
                            "cell": cell,
                            "seed": seed,
                            "cached": True,
                            "summary": compact_summary(cached[h]),
                        }
                    )
                continue
            payloads.append(
                orjson.dumps(
                    {
                        "a": str(a["id"]),
                        "c": cell,
                        "s": seed,
                        "h": h,
                        "sys": str(a["system_id"]),
                        "v": a["system_version"],
                        "sh": a["spec_hash"],
                        "sc": scenario,
                        "p": int(a["priority"]),
                        "q": time.time(),
                        "o": str(a["owner_id"]),
                    }
                )
            )
        if hits:
            SIMS.labels("cached").inc(hits)
            await conn.execute(
                "UPDATE sim_results SET hits = hits + 1, last_hit_at = now() WHERE task_hash = ANY($1::text[])",
                list(cached),
            )
            await conn.execute(
                """UPDATE analyses SET sims_done = sims_done + $2, sims_cached = sims_cached + $2,
                          first_result_at = COALESCE(first_result_at, now()) WHERE id = $1""",
                a["id"],
                hits,
            )
        await enqueue(redis, tenant, int(a["priority"]), payloads)
        if payloads:
            return False, events
    return False, events


async def finalize(conn: asyncpg.Connection, a: asyncpg.Record) -> dict[str, Any] | None:
    params = SweepParams.from_dict(a["params"])
    evaluated, aggs = await cell_states(conn, a["id"], params)
    verdicts = infer(params, {c: bool(v) for c, v in evaluated.items()})
    rows = boundary(params, verdicts)
    summary = summarize(params, verdicts, aggs, rows)
    sav = savings(params, len(evaluated))
    result = {
        **summary,
        "savings": sav,
        "verdicts": [verdicts[c] for c in range(params.n_cells)],
        "cells": {str(c): aggs[c] for c in sorted(aggs)},
    }
    row = await conn.fetchrow(
        """UPDATE analyses SET status = 'done', finished_at = now(), cells_done = $2, result = $3
           WHERE id = $1 AND status = 'running' RETURNING created_at, finished_at""",
        a["id"],
        len(evaluated),
        result,
    )
    if row is None:
        return None
    ANALYSES_FINISHED.labels("done").inc()
    ANALYSIS_DURATION.observe((row["finished_at"] - row["created_at"]).total_seconds())
    SIMS_AVOIDED.inc(max(0, sav["full_grid_sims"] - sav["simulated_sims"]))
    await conn.execute(
        "INSERT INTO activity_log (user_id, kind, data) VALUES ($1, 'analysis.finished', $2)",
        a["owner_id"],
        {"analysis": str(a["id"]), "safe_fraction": summary["safe_fraction"]},
    )
    return result


async def advance(
    db_conn: asyncpg.Connection,
    redis: aioredis.Redis,
    analysis_id: str,
    settings: Settings,
    engine_version: str,
    *,
    start: bool = False,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async with db_conn.transaction():
        a = await db_conn.fetchrow("SELECT * FROM analyses WHERE id = $1 FOR UPDATE", analysis_id)
        if a is None:
            return events
        status = str(a["status"])
        if start and status == "queued":
            a = await db_conn.fetchrow(
                "UPDATE analyses SET status = 'running', started_at = now() WHERE id = $1 RETURNING *", analysis_id
            )
            events.append({"type": "status", "status": "running"})
        elif status != "running":
            return events
        complete, evs = await schedule_more(db_conn, redis, a, settings, engine_version)
        events.extend(evs)
        if complete:
            result = await finalize(db_conn, a)
            if result is not None:
                events.append({"type": "status", "status": "done", "result": result})
    return events


async def publish(redis: aioredis.Redis, analysis_id: str, events: list[dict[str, Any]]) -> None:
    for ev in events:
        terminal = ev.get("type") == "status" and ev.get("status") in ("done", "failed", "cancelled")
        await emit_analysis(redis, analysis_id, ev, ttl_s=PROGRESS_TTL_S if terminal else None)
