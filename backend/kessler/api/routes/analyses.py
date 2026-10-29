from __future__ import annotations

import secrets
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from ... import keys
from ...analysis.planner import TRIGGER_DEFAULTS, SweepParams, aggregate_cell, default_params, linspace
from ...db import newer_lsn
from ...errors import ApiError
from ...metrics import ADMISSION_REJECTED, ANALYSES_SUBMITTED
from ...security import sign_stream_token
from ...spec import Finding, PatchError, apply_patch, check, merge_patches
from ...tracing import inject
from ..common import JSON, body_hash, decode_cursor, encode_cursor, idempotent, log_activity
from ..context import AppContext
from ..deps import Ctx, IdempotencyKey, MinLsn, User, limit, parse_uuid

router = APIRouter(prefix="/api/v1", tags=["analyses"])

TriggerKind = Literal["latency", "errors", "crash", "spike", "cache_flush", "capacity_loss"]


class TriggerIn(BaseModel):
    kind: TriggerKind
    target: str | None = Field(None, max_length=64)
    duration_s: float | None = Field(None, ge=0, le=600)
    magnitude: float | None = Field(None, ge=0, le=1000)
    count: int | None = Field(None, ge=0, le=1000)


class RangeIn(BaseModel):
    min: float = Field(ge=0, le=1000)
    max: float = Field(ge=0, le=1000)
    steps: int = Field(ge=1, le=16)


class AnalysisIn(BaseModel):
    system_id: str
    version: int | None = Field(None, ge=1)
    trigger: TriggerIn | None = None
    loads: RangeIn | None = None
    values: RangeIn | None = None
    seeds: int = Field(3, ge=1, le=8)
    mode: Literal["adaptive", "grid"] = "adaptive"
    priority: Literal["interactive", "batch"] = "interactive"


class ExperimentIn(BaseModel):
    finding_ids: list[str] | None = Field(None, max_length=50)
    patch: list[dict[str, Any]] | None = Field(None, max_length=50)


_backlog_cache: tuple[float, int] = (0.0, 0)


async def queue_backlog(ctx: AppContext) -> int:
    global _backlog_cache
    now = time.monotonic()
    if now - _backlog_cache[0] < 1.0:
        return _backlog_cache[1]
    total = 0
    try:
        tenants = await ctx.redis.zrange(keys.TENANTS, 0, -1)
        if tenants:
            pipe = ctx.redis.pipeline(transaction=False)
            for t in tenants:
                pipe.llen(keys.tenant_queue(t.decode()))
            total = sum(await pipe.execute())
    except Exception:
        total = 0
    _backlog_cache = (now, total)
    return total


def default_trigger(checks: dict[str, Any]) -> dict[str, Any]:
    topo = checks.get("topology", {})
    bottleneck, entry = topo.get("bottleneck"), topo.get("entry")
    if bottleneck and bottleneck != entry:
        return {"kind": "latency", "target": bottleneck, "duration_s": 20.0}
    return {"kind": "spike", "duration_s": 30.0}


def reference_load(checks: dict[str, Any], trigger: dict[str, Any]) -> float:
    analysis = checks["analysis"]
    services = {s["name"]: s for s in analysis["services"]}
    rps = max(float(analysis.get("offered_rps") or 0), 1e-9)
    target = trigger.get("target")
    if trigger["kind"] == "cache_flush" and target in services:
        edges = [e for e in analysis["edges"] if e["from"] == target]
        cache_rps = services[target]["arrival_rps"]
        best = None
        for e in edges:
            cap = services.get(e["to"], {}).get("capacity_rps", 0)
            cold = cache_rps
            if cap and cold:
                ratio = cap / cold
                best = ratio if best is None else min(best, ratio)
        return best or float(analysis.get("max_load_multiplier") or 1.0)
    if target in services and services[target]["visits"] > 0:
        return services[target]["capacity_rps"] / (services[target]["visits"] * rps)
    return float(analysis.get("max_load_multiplier") or 1.0)


def build_params(body: AnalysisIn, checks: dict[str, Any], seed_base: int) -> SweepParams:
    trigger = body.trigger.model_dump(exclude_none=True) if body.trigger else default_trigger(checks)
    services = {n["id"] for n in checks.get("topology", {}).get("nodes", []) if n["kind"] != "client"}
    caches = {n["id"] for n in checks.get("topology", {}).get("nodes", []) if n["kind"] == "cache"}
    kind = trigger["kind"]
    if kind != "spike":
        target = trigger.get("target")
        if not target or target not in services:
            raise ApiError(422, "bad_trigger", f"Pick which service the {kind} trigger hits", field="trigger.target")
        if kind == "cache_flush" and target not in caches:
            raise ApiError(422, "bad_trigger", f"'{target}' is not a cache", field="trigger.target")
    ref = reference_load(checks, trigger)
    if not ref > 0:
        ref = 1.0
    p = default_params(trigger, ref, seeds=body.seeds, mode=body.mode)
    if body.loads:
        loads = linspace(body.loads.min, body.loads.max, body.loads.steps)
    else:
        lo, hi = min(0.2 * ref, 0.5), max(1.2 * ref, 1.2)
        loads = linspace(round(lo, 3), round(hi, 3), 10)
    values = p.values
    if body.values and kind != "cache_flush":
        values = linspace(body.values.min, body.values.max, body.values.steps)
    if any(v <= 0 for v in loads):
        raise ApiError(422, "bad_range", "Loads must be positive")
    if kind == "latency" and any(v < 1 for v in values):
        raise ApiError(422, "bad_range", "Latency multipliers start at 1×")
    return SweepParams(
        trigger=p.trigger,
        loads=loads,
        values=values,
        axis2=p.axis2,
        seeds=body.seeds,
        mode=body.mode,
        base_seed=seed_base,
    )


def estimated_sims(p: SweepParams) -> int:
    if p.mode == "grid":
        return p.n_cells * p.seeds
    per_row = min(p.n_cols, 3 + max(1, (p.n_cols - 1).bit_length()))
    return p.n_rows * per_row * p.seeds


def analysis_json(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["id"] = str(d["id"])
    d["system_id"] = str(d["system_id"])
    d["owner_id"] = str(d["owner_id"])
    if d.get("parent_id"):
        d["parent_id"] = str(d["parent_id"])
    d["status"] = str(d["status"])
    if d.get("first_result_at") and d.get("created_at"):
        d["time_to_first_insight_s"] = round((d["first_result_at"] - d["created_at"]).total_seconds(), 3)
    return d


def stream_info(ctx: AppContext, user_id: str, topic: str, last_id: str, perms: str = "r") -> dict[str, Any]:
    return {"topic": topic, "token": sign_stream_token(ctx.settings, topic, user_id, perms), "last_id": last_id}


async def last_stream_id(ctx: AppContext, key: str) -> str:
    try:
        entries = await ctx.redis.xrevrange(key, count=1)
        return entries[0][0].decode() if entries else "0-0"
    except Exception:
        return "0-0"


async def _insert_analysis(
    ctx: AppContext,
    conn: Any,
    user_id: str,
    sys_id: uuid.UUID,
    version: int,
    spec_hash: str,
    params: SweepParams,
    priority: int,
    parent_id: uuid.UUID | None,
) -> Any:
    await conn.execute("SELECT 1 FROM users WHERE id = $1 FOR UPDATE", uuid.UUID(user_id))
    active = await conn.fetchval(
        "SELECT count(*) FROM analyses WHERE owner_id = $1 AND status IN ('queued', 'running')", uuid.UUID(user_id)
    )
    if active >= ctx.settings.max_active_analyses_per_user:
        raise ApiError(
            429,
            "too_many_active",
            f"You already have {active} analyses running",
            "Wait for one to finish or cancel it.",
            headers={"Retry-After": "10"},
        )
    row = await conn.fetchrow(
        """INSERT INTO analyses (owner_id, system_id, system_version, spec_hash, parent_id, params,
                                 priority, cells_total)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *""",
        uuid.UUID(user_id),
        sys_id,
        version,
        spec_hash,
        parent_id,
        params.as_dict(),
        priority,
        params.n_cells,
    )
    await conn.execute(
        "INSERT INTO outbox (topic, payload) VALUES ('analysis.created', $1)",
        {"id": str(row["id"]), "trace": inject()},
    )
    return row


@router.post("/analyses", status_code=202)
async def create_analysis(body: AnalysisIn, user: User, ctx: Ctx, idempotency_key: IdempotencyKey = None) -> Response:
    await limit(ctx, "submit", user.user_id)
    backlog = await queue_backlog(ctx)
    if backlog > ctx.settings.admission_queue_limit:
        ADMISSION_REJECTED.labels("backlog").inc()
        raise ApiError(
            503, "busy", "Kessler is busy right now", "Try again in 30 seconds.", headers={"Retry-After": "30"}
        )
    sid = parse_uuid(body.system_id, "system")
    async with ctx.db.primary_conn() as conn:
        sys_row = await conn.fetchrow(
            "SELECT * FROM systems WHERE id = $1 AND owner_id = $2 AND deleted_at IS NULL", sid, uuid.UUID(user.user_id)
        )
        if sys_row is None:
            raise ApiError(404, "not_found", "No such system")
        version = body.version or sys_row["latest_version"]
        ver = await conn.fetchrow("SELECT * FROM system_versions WHERE system_id = $1 AND version = $2", sid, version)
    if ver is None:
        raise ApiError(404, "not_found", "No such version")
    params = build_params(body, ver["checks"], seed_base=1)
    sims = estimated_sims(params)
    if sims > ctx.settings.max_sims_per_analysis:
        raise ApiError(
            422,
            "too_large",
            f"This sweep needs about {sims} simulations; the limit is {ctx.settings.max_sims_per_analysis}",
            "Use fewer steps or seeds, or adaptive mode.",
        )
    priority = 2 if (body.priority == "batch" or user.via == "api_key") else 1

    async def handler(conn: Any) -> tuple[int, dict[str, Any]]:
        row = await _insert_analysis(ctx, conn, user.user_id, sid, version, ver["spec_hash"], params, priority, None)
        await log_activity(conn, user.user_id, "analysis.submitted", {"analysis": str(row["id"]), "mode": body.mode})
        return 202, {"analysis": analysis_json(row)}

    status, out, replayed, lsn = await idempotent(
        ctx, user.user_id, idempotency_key, body_hash(body.model_dump()), handler
    )
    if not replayed:
        ANALYSES_SUBMITTED.labels(body.mode).inc()
    aid = out["analysis"]["id"]
    out["stream"] = stream_info(ctx, user.user_id, f"analysis:{aid}", "0-0")
    out["estimated_sims"] = sims
    headers = (
        {"location": f"/api/v1/analyses/{aid}", "x-kessler-lsn": lsn}
        if lsn
        else {"location": f"/api/v1/analyses/{aid}"}
    )
    if replayed:
        headers["idempotent-replayed"] = "true"
    return JSON(out, status_code=status, headers=headers)


@router.get("/analyses")
async def list_analyses(
    user: User,
    ctx: Ctx,
    min_lsn: MinLsn = None,
    system_id: str | None = None,
    cursor: str | None = Query(None, max_length=200),
    limit_: int = Query(20, alias="limit", ge=1, le=100),
) -> dict[str, Any]:
    after = decode_cursor(cursor)
    sid = parse_uuid(system_id, "system") if system_id else None
    async with ctx.db.read(min_lsn) as conn:
        rows = await conn.fetch(
            """SELECT a.id, a.system_id, a.system_version, a.status, a.params, a.cells_total, a.cells_done,
                      a.sims_done, a.sims_cached, a.created_at, a.finished_at, a.parent_id, a.first_result_at,
                      a.owner_id, a.result->'safe_fraction' AS safe_fraction, s.name AS system_name
               FROM analyses a JOIN systems s ON s.id = a.system_id
               WHERE a.owner_id = $1 AND ($2::uuid IS NULL OR a.system_id = $2)
                 AND ($3::timestamptz IS NULL OR (a.created_at, a.id) < ($3, $4::uuid))
               ORDER BY a.created_at DESC, a.id DESC LIMIT $5""",
            uuid.UUID(user.user_id),
            sid,
            after[0] if after else None,
            uuid.UUID(after[1]) if after else None,
            limit_ + 1,
        )
    items = [analysis_json(r) for r in rows[:limit_]]
    nxt = encode_cursor(rows[limit_ - 1]["created_at"], rows[limit_ - 1]["id"]) if len(rows) > limit_ else None
    return {"items": items, "next_cursor": nxt}


async def _owned_analysis(ctx: AppContext, user_id: str, analysis_id: str, min_lsn: str | None = None) -> Any:
    async with ctx.db.read(min_lsn) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM analyses WHERE id = $1 AND owner_id = $2",
            parse_uuid(analysis_id, "analysis"),
            uuid.UUID(user_id),
        )
    if row is None:
        raise ApiError(404, "not_found", "No such analysis")
    return row


def cells_view(params: SweepParams, tasks: list[Any]) -> list[dict[str, Any]]:
    by_cell: dict[int, list[Any]] = {}
    for t in tasks:
        by_cell.setdefault(t["cell"], []).append(t)
    out = []
    for cell, ts in sorted(by_cell.items()):
        row, col = params.coords(cell)
        done = [t["summary"] for t in ts if t["status"] == "done" and t["summary"]]
        entry: dict[str, Any] = {
            "cell": cell,
            "row": row,
            "col": col,
            "load": params.loads[col],
            "value": params.values[row],
            "seeds_done": len(done),
            "seeds_total": params.seeds,
            "cached": sum(1 for t in ts if t["cached"]),
            "summaries": [
                {**t["summary"], "seed": t["seed_index"]}
                for t in ts
                if t["status"] in ("done", "failed") and t["summary"]
            ],
        }
        if len(done) == params.seeds:
            entry["aggregate"] = aggregate_cell(done)
        out.append(entry)
    return out


@router.get("/analyses/{analysis_id}")
async def get_analysis(analysis_id: str, user: User, ctx: Ctx, min_lsn: MinLsn = None) -> dict[str, Any]:
    aid = parse_uuid(analysis_id, "analysis")
    last_id = await last_stream_id(ctx, keys.progress_stream(str(aid)))
    fence = newer_lsn(min_lsn, await ctx.db.fence())
    row = await _owned_analysis(ctx, user.user_id, analysis_id, fence)
    params = SweepParams.from_dict(row["params"])
    async with ctx.db.read(fence) as conn:
        tasks = await conn.fetch(
            "SELECT cell, seed_index, status, cached, summary FROM analysis_tasks WHERE analysis_id = $1", row["id"]
        )
        system = await conn.fetchrow(
            """SELECT s.name, v.version, v.spec_yaml, v.checks FROM systems s
               JOIN system_versions v ON v.system_id = s.id AND v.version = $2 WHERE s.id = $1""",
            row["system_id"],
            row["system_version"],
        )
    return {
        "analysis": analysis_json(row),
        "params": params.as_dict(),
        "cells": cells_view(params, tasks),
        "system": dict(system) if system else None,
        "stream": stream_info(ctx, user.user_id, f"analysis:{row['id']}", last_id),
    }


@router.post("/analyses/{analysis_id}/cancel", status_code=202)
async def cancel_analysis(analysis_id: str, user: User, ctx: Ctx) -> dict[str, Any]:
    aid = parse_uuid(analysis_id, "analysis")
    async with ctx.db.write() as conn:
        done = await conn.fetchval(
            """UPDATE analyses SET status = 'cancelled', finished_at = now()
               WHERE id = $1 AND owner_id = $2 AND status IN ('queued', 'running') RETURNING 1""",
            aid,
            uuid.UUID(user.user_id),
        )
        if not done:
            raise ApiError(409, "not_running", "The analysis is not running")
        await conn.execute("INSERT INTO outbox (topic, payload) VALUES ('analysis.cancelled', $1)", {"id": str(aid)})
    await ctx.redis.set(f"{keys.PREFIX}:cancelled:{aid}", b"1", ex=86_400)
    return {"status": "cancelled"}


@router.post("/analyses/{analysis_id}/experiments", status_code=202)
async def create_experiment(
    analysis_id: str, body: ExperimentIn, user: User, ctx: Ctx, idempotency_key: IdempotencyKey = None
) -> Response:
    await limit(ctx, "submit", user.user_id)
    parent = await _owned_analysis(ctx, user.user_id, analysis_id)
    async with ctx.db.primary_conn() as conn:
        ver = await conn.fetchrow(
            "SELECT * FROM system_versions WHERE system_id = $1 AND version = $2",
            parent["system_id"],
            parent["system_version"],
        )
    findings = [Finding(**{k: v for k, v in f.items() if k != "id"}) for f in ver["checks"].get("findings", [])]
    if body.patch:
        ops = body.patch
    else:
        chosen = [f for f in findings if body.finding_ids is None or f.id in body.finding_ids]
        ops = merge_patches(chosen)
    if not ops:
        raise ApiError(422, "nothing_to_apply", "There are no fixes to apply")
    try:
        new_yaml = apply_patch(ver["spec_yaml"], ops)
    except PatchError as exc:
        raise ApiError(422, "bad_patch", str(exc)) from None
    result = check(new_yaml)
    if not result["ok"]:
        raise ApiError(422, "invalid_spec", "The fix produced an invalid spec", errors=result["errors"])
    params = SweepParams.from_dict(parent["params"])

    async def handler(conn: Any) -> tuple[int, dict[str, Any]]:
        sys_row = await conn.fetchrow(
            """UPDATE systems SET latest_version = latest_version + 1, updated_at = now()
               WHERE id = $1 AND owner_id = $2 AND deleted_at IS NULL RETURNING *""",
            parent["system_id"],
            uuid.UUID(user.user_id),
        )
        if sys_row is None:
            raise ApiError(404, "not_found", "The system no longer exists")
        checks_doc = {k: result[k] for k in ("analysis", "findings", "score", "topology", "description") if k in result}
        checks_doc["experiment_of"] = {"analysis": str(parent["id"]), "version": parent["system_version"], "ops": ops}
        await conn.execute(
            """INSERT INTO system_versions (system_id, version, spec_yaml, spec_json, spec_hash, checks)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            parent["system_id"],
            sys_row["latest_version"],
            new_yaml,
            result["system"],
            result["spec_hash"],
            checks_doc,
        )
        row = await _insert_analysis(
            ctx,
            conn,
            user.user_id,
            parent["system_id"],
            sys_row["latest_version"],
            result["spec_hash"],
            params,
            int(parent["priority"]),
            parent["id"],
        )
        await log_activity(conn, user.user_id, "experiment.created", {"parent": str(parent["id"]), "ops": len(ops)})
        return 202, {"analysis": analysis_json(row), "version": sys_row["latest_version"], "yaml": new_yaml}

    status, out, replayed, lsn = await idempotent(
        ctx, user.user_id, idempotency_key, body_hash({"parent": analysis_id, **body.model_dump()}), handler
    )
    aid = out["analysis"]["id"]
    out["stream"] = stream_info(ctx, user.user_id, f"analysis:{aid}", "0-0")
    headers = {"location": f"/api/v1/analyses/{aid}"}
    if lsn:
        headers["x-kessler-lsn"] = lsn
    if replayed:
        headers["idempotent-replayed"] = "true"
    return JSON(out, status_code=status, headers=headers)


@router.post("/analyses/{analysis_id}/report", status_code=201)
async def create_report(analysis_id: str, user: User, ctx: Ctx) -> dict[str, Any]:
    row = await _owned_analysis(ctx, user.user_id, analysis_id)
    if str(row["status"]) != "done":
        raise ApiError(409, "not_finished", "Only finished analyses can be shared")
    data = await get_analysis(analysis_id, user, ctx)
    snapshot = {
        "analysis": {
            k: data["analysis"][k]
            for k in (
                "id",
                "status",
                "created_at",
                "finished_at",
                "result",
                "params",
                "sims_done",
                "sims_cached",
                "cells_total",
                "parent_id",
            )
            if k in data["analysis"]
        },
        "cells": data["cells"],
        "system": data["system"],
        "author": user.handle,
    }
    async with ctx.db.write() as conn:
        slug = await conn.fetchval(
            """INSERT INTO reports (slug, analysis_id, snapshot) VALUES ($1, $2, $3)
               ON CONFLICT (analysis_id) DO UPDATE SET analysis_id = EXCLUDED.analysis_id
               RETURNING slug""",
            secrets.token_urlsafe(8),
            row["id"],
            snapshot,
        )
        await log_activity(conn, user.user_id, "report.created", {"analysis": str(row["id"])})
    return {"slug": slug, "url": f"{ctx.settings.public_url}/r/{slug}"}


@router.get("/reports/{slug}")
async def get_report(slug: str, ctx: Ctx) -> Response:
    async with ctx.db.read() as conn:
        snap = await conn.fetchval("SELECT snapshot FROM reports WHERE slug = $1", slug[:32])
    if snap is None:
        raise ApiError(404, "not_found", "No such report")
    return JSON(snap, headers={"cache-control": "public, max-age=31536000, immutable", "etag": f'"{slug}"'})


__all__ = ["TRIGGER_DEFAULTS", "router"]
