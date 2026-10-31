from __future__ import annotations

import secrets
import time
import uuid
from typing import Any

import orjson
from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from ... import keys
from ...db import newer_lsn
from ...errors import ApiError
from ...tracing import inject
from ..common import JSON, body_hash, idempotent, log_activity
from ..deps import Ctx, IdempotencyKey, MinLsn, User, limit, parse_uuid
from .analyses import last_stream_id, stream_info

router = APIRouter(prefix="/api/v1", tags=["runs"])


class RunIn(BaseModel):
    system_id: str
    version: int | None = Field(None, ge=1)
    load: float = Field(1.0, gt=0, le=20)
    speed: float = Field(4.0, ge=1, le=20)
    horizon_s: float = Field(600, ge=60, le=3600)
    seed: int | None = Field(None, ge=1, le=2**31)


class CommandIn(BaseModel):
    command: dict[str, Any]


def run_json(r: Any) -> dict[str, Any]:
    d = dict(r)
    for k in ("id", "system_id", "owner_id"):
        d[k] = str(d[k])
    d["status"] = str(d["status"])
    return d


@router.post("/runs", status_code=202)
async def create_run(body: RunIn, user: User, ctx: Ctx, idempotency_key: IdempotencyKey = None) -> Response:
    await limit(ctx, "submit", user.user_id)
    sid = parse_uuid(body.system_id, "system")
    async with ctx.db.primary_conn() as conn:
        sys_row = await conn.fetchrow(
            "SELECT * FROM systems WHERE id = $1 AND owner_id = $2 AND deleted_at IS NULL", sid, uuid.UUID(user.user_id)
        )
        if sys_row is None:
            raise ApiError(404, "not_found", "No such system")
        version = body.version or sys_row["latest_version"]
        spec_hash = await conn.fetchval(
            "SELECT spec_hash FROM system_versions WHERE system_id = $1 AND version = $2", sid, version
        )
    if spec_hash is None:
        raise ApiError(404, "not_found", "No such version")
    params = {
        "load": body.load,
        "speed": body.speed,
        "horizon_s": body.horizon_s,
        "seed": body.seed or secrets.randbelow(2**31 - 1) + 1,
        "warmup_s": 0,
    }

    async def handler(conn: Any) -> tuple[int, dict[str, Any]]:
        await conn.execute("SELECT 1 FROM users WHERE id = $1 FOR UPDATE", uuid.UUID(user.user_id))
        active = await conn.fetchval(
            "SELECT count(*) FROM runs WHERE owner_id = $1 AND status IN ('queued', 'running')", uuid.UUID(user.user_id)
        )
        if active >= ctx.settings.max_live_runs_per_user:
            raise ApiError(429, "too_many_runs", f"You already have {active} live runs", "Stop one first.")
        row = await conn.fetchrow(
            """INSERT INTO runs (owner_id, system_id, system_version, spec_hash, params)
               VALUES ($1, $2, $3, $4, $5) RETURNING *""",
            uuid.UUID(user.user_id),
            sid,
            version,
            spec_hash,
            params,
        )
        await conn.execute(
            "INSERT INTO outbox (topic, payload) VALUES ('run.created', $1)", {"id": str(row["id"]), "trace": inject()}
        )
        await log_activity(conn, user.user_id, "run.started", {"run": str(row["id"])})
        return 202, {"run": run_json(row)}

    status, out, replayed, lsn = await idempotent(
        ctx, user.user_id, idempotency_key, body_hash(body.model_dump()), handler
    )
    rid = out["run"]["id"]
    out["stream"] = stream_info(ctx, user.user_id, f"run:{rid}", "0-0", perms="rw")
    headers = {"location": f"/api/v1/runs/{rid}"}
    if lsn:
        headers["x-kessler-lsn"] = lsn
    if replayed:
        headers["idempotent-replayed"] = "true"
    return JSON(out, status_code=status, headers=headers)


async def _owned_run(ctx: Ctx, user_id: str, run_id: str, min_lsn: str | None = None) -> Any:
    row = await ctx.db.fetchrow_or_primary(
        min_lsn, "SELECT * FROM runs WHERE id = $1 AND owner_id = $2", parse_uuid(run_id, "run"), uuid.UUID(user_id)
    )
    if row is None:
        raise ApiError(404, "not_found", "No such run")
    return row


@router.get("/runs/{run_id}")
async def get_run(run_id: str, user: User, ctx: Ctx, min_lsn: MinLsn = None) -> dict[str, Any]:
    rid = parse_uuid(run_id, "run")
    last_id = await last_stream_id(ctx, keys.run_ticks(str(rid)))
    fence = newer_lsn(min_lsn, await ctx.db.fence())
    row = await _owned_run(ctx, user.user_id, run_id, fence)
    async with ctx.db.read(fence) as conn:
        cmds = await conn.fetch(
            "SELECT seq, sim_time, command, created_at FROM run_commands WHERE run_id = $1 ORDER BY seq DESC LIMIT 100",
            row["id"],
        )
        topo = await conn.fetchval(
            "SELECT checks->'topology' FROM system_versions WHERE system_id = $1 AND version = $2",
            row["system_id"],
            row["system_version"],
        )
    return {
        "run": run_json(row),
        "commands": [dict(c) for c in reversed(cmds)],
        "topology": topo,
        "stream": stream_info(ctx, user.user_id, f"run:{row['id']}", last_id, perms="rw"),
    }


@router.post("/runs/{run_id}/commands", status_code=202)
async def send_command(run_id: str, body: CommandIn, user: User, ctx: Ctx) -> dict[str, Any]:
    await limit(ctx, "control", user.user_id)
    row = await _owned_run(ctx, user.user_id, run_id)
    if str(row["status"]) not in ("queued", "running"):
        raise ApiError(409, "run_finished", "This run has finished")
    req_id = secrets.token_hex(8)
    await ctx.redis.xadd(
        keys.run_ctl(str(row["id"])),
        {
            b"cmd": orjson.dumps(body.command),
            b"req": req_id.encode(),
            b"user": user.user_id.encode(),
            b"ts": str(time.time()).encode(),
        },
        maxlen=1000,
        approximate=True,
    )
    return {"req": req_id}


@router.post("/runs/{run_id}/stop", status_code=202)
async def stop_run(run_id: str, user: User, ctx: Ctx) -> dict[str, Any]:
    row = await _owned_run(ctx, user.user_id, run_id)
    async with ctx.db.write() as conn:
        cancelled = await conn.fetchval(
            "UPDATE runs SET status = 'cancelled', ended_at = now() WHERE id = $1 AND status = 'queued' RETURNING 1",
            row["id"],
        )
    if not cancelled:
        await ctx.redis.xadd(
            keys.run_ctl(str(row["id"])),
            {
                b"cmd": orjson.dumps({"op": "stop"}),
                b"req": b"stop",
                b"user": user.user_id.encode(),
                b"ts": str(time.time()).encode(),
            },
            maxlen=1000,
            approximate=True,
        )
    return {"status": "stopping"}
