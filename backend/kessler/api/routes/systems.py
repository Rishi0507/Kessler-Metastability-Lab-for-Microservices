from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from ...errors import ApiError
from ...spec import PatchError, apply_patch, check
from ...spec.otel import TraceImportError, import_otlp
from ...spec.templates import all_templates
from ..common import JSON, decode_cursor, encode_cursor, etag_for_version, log_activity, parse_if_match
from ..deps import Ctx, IfMatch, IfNoneMatch, MinLsn, User, limit, parse_uuid

router = APIRouter(prefix="/api/v1", tags=["systems"])


class CheckIn(BaseModel):
    yaml: str = Field(max_length=64 * 1024)


class SystemIn(BaseModel):
    yaml: str = Field(max_length=64 * 1024)
    name: str | None = Field(None, max_length=80)
    template_id: str | None = Field(None, max_length=64)


def _checks_doc(result: dict[str, Any]) -> dict[str, Any]:
    keep = ("analysis", "findings", "score", "topology", "description")
    return {k: result[k] for k in keep if k in result}


def _validated(yaml_text: str) -> dict[str, Any]:
    result = check(yaml_text)
    if not result["ok"]:
        raise ApiError(422, "invalid_spec", "The spec has errors", errors=result["errors"])
    return result


def _system_json(sys_row: Any, ver_row: Any) -> dict[str, Any]:
    return {
        "id": str(sys_row["id"]),
        "name": sys_row["name"],
        "version": ver_row["version"],
        "latest_version": sys_row["latest_version"],
        "created_at": sys_row["created_at"],
        "updated_at": sys_row["updated_at"],
        "yaml": ver_row["spec_yaml"],
        "spec_hash": ver_row["spec_hash"],
        "checks": ver_row["checks"],
    }


@router.get("/templates")
async def templates() -> Response:
    return JSON(
        {"templates": [t.as_dict() for t in all_templates()]},
        headers={"cache-control": "public, max-age=300"},
    )


@router.post("/specs/check")
async def check_spec(body: CheckIn, user: User, ctx: Ctx) -> dict[str, Any]:
    await limit(ctx, "read", user.user_id)
    return check(body.yaml)


class ImportIn(BaseModel):
    traces: str = Field(max_length=8 * 1024 * 1024)
    name: str = Field("Imported system", min_length=1, max_length=80)


@router.post("/specs/import")
async def import_spec(body: ImportIn, user: User, ctx: Ctx) -> dict[str, Any]:
    await limit(ctx, "write", user.user_id)
    try:
        text, notes = import_otlp(body.traces, body.name)
    except TraceImportError as e:
        raise ApiError(422, "invalid_traces", str(e)) from None
    return {"yaml": text, "notes": notes}


class ApplyIn(BaseModel):
    yaml: str = Field(max_length=64 * 1024)
    ops: list[dict[str, Any]] = Field(max_length=50)


@router.post("/specs/apply")
async def apply_fix(body: ApplyIn, user: User, ctx: Ctx) -> dict[str, Any]:
    await limit(ctx, "read", user.user_id)
    try:
        new_yaml = apply_patch(body.yaml, body.ops)
    except PatchError as exc:
        raise ApiError(422, "bad_patch", str(exc)) from None
    return {"yaml": new_yaml, "check": check(new_yaml)}


@router.get("/systems")
async def list_systems(
    user: User,
    ctx: Ctx,
    min_lsn: MinLsn = None,
    cursor: str | None = Query(None, max_length=200),
    limit_: int = Query(20, alias="limit", ge=1, le=100),
) -> dict[str, Any]:
    after = decode_cursor(cursor)
    async with ctx.db.read(min_lsn) as conn:
        rows = await conn.fetch(
            """SELECT s.id, s.name, s.latest_version, s.created_at, s.updated_at,
                      v.spec_hash, v.checks->'score' AS score,
                      (SELECT count(*) FROM analyses a WHERE a.system_id = s.id) AS analyses
               FROM systems s
               JOIN system_versions v ON v.system_id = s.id AND v.version = s.latest_version
               WHERE s.owner_id = $1 AND s.deleted_at IS NULL
                 AND ($2::timestamptz IS NULL OR (s.updated_at, s.id) < ($2, $3::uuid))
               ORDER BY s.updated_at DESC, s.id DESC
               LIMIT $4""",
            uuid.UUID(user.user_id),
            after[0] if after else None,
            uuid.UUID(after[1]) if after else None,
            limit_ + 1,
        )
    items = [dict(r) | {"id": str(r["id"])} for r in rows[:limit_]]
    next_cursor = encode_cursor(rows[limit_ - 1]["updated_at"], rows[limit_ - 1]["id"]) if len(rows) > limit_ else None
    return {"items": items, "next_cursor": next_cursor}


@router.post("/systems", status_code=201)
async def create_system(body: SystemIn, user: User, ctx: Ctx) -> Response:
    await limit(ctx, "write", user.user_id)
    result = _validated(body.yaml)
    name = (body.name or result["name"]).strip()[:80]
    async with ctx.db.write() as conn:
        sys_row = await conn.fetchrow(
            "INSERT INTO systems (owner_id, name) VALUES ($1, $2) RETURNING *", uuid.UUID(user.user_id), name
        )
        ver_row = await conn.fetchrow(
            """INSERT INTO system_versions (system_id, version, spec_yaml, spec_json, spec_hash, checks)
               VALUES ($1, 1, $2, $3, $4, $5) RETURNING *""",
            sys_row["id"],
            body.yaml,
            result["system"],
            result["spec_hash"],
            _checks_doc(result),
        )
        await log_activity(conn, user.user_id, "system.created", {"template": body.template_id})
        lsn = await ctx.db.current_lsn(conn)
    return JSON(
        _system_json(sys_row, ver_row),
        status_code=201,
        headers={"etag": etag_for_version(1), "location": f"/api/v1/systems/{sys_row['id']}", "x-kessler-lsn": lsn},
    )


async def _load(ctx: Ctx, user_id: str, system_id: uuid.UUID, version: int | None, min_lsn: str | None):
    sys_row = await ctx.db.fetchrow_or_primary(
        min_lsn,
        "SELECT * FROM systems WHERE id = $1 AND owner_id = $2 AND deleted_at IS NULL",
        system_id,
        uuid.UUID(user_id),
    )
    if sys_row is None:
        raise ApiError(404, "not_found", "No such system")
    ver_row = await ctx.db.fetchrow_or_primary(
        min_lsn,
        "SELECT * FROM system_versions WHERE system_id = $1 AND version = $2",
        system_id,
        version or sys_row["latest_version"],
    )
    if ver_row is None:
        raise ApiError(404, "not_found", "No such version")
    return sys_row, ver_row


@router.get("/systems/{system_id}")
async def get_system(
    system_id: str, user: User, ctx: Ctx, min_lsn: MinLsn = None, if_none_match: IfNoneMatch = None
) -> Response:
    sys_row, ver_row = await _load(ctx, user.user_id, parse_uuid(system_id, "system"), None, min_lsn)
    etag = etag_for_version(sys_row["latest_version"])
    if if_none_match and if_none_match.strip() == etag:
        return Response(status_code=304, headers={"etag": etag})
    return JSON(_system_json(sys_row, ver_row), headers={"etag": etag, "cache-control": "private, no-cache"})


@router.get("/systems/{system_id}/versions/{version}")
async def get_version(system_id: str, version: int, user: User, ctx: Ctx, min_lsn: MinLsn = None) -> Response:
    sys_row, ver_row = await _load(ctx, user.user_id, parse_uuid(system_id, "system"), version, min_lsn)
    return JSON(_system_json(sys_row, ver_row), headers={"cache-control": "private, max-age=86400, immutable"})


@router.get("/systems/{system_id}/versions")
async def list_versions(system_id: str, user: User, ctx: Ctx, min_lsn: MinLsn = None) -> dict[str, Any]:
    sid = parse_uuid(system_id, "system")
    await _load(ctx, user.user_id, sid, None, min_lsn)
    async with ctx.db.read(min_lsn) as conn:
        rows = await conn.fetch(
            """SELECT version, spec_hash, created_at, checks->'score' AS score
               FROM system_versions WHERE system_id = $1 ORDER BY version DESC LIMIT 100""",
            sid,
        )
    return {"items": [dict(r) for r in rows]}


@router.put("/systems/{system_id}")
async def update_system(system_id: str, body: SystemIn, user: User, ctx: Ctx, if_match: IfMatch = None) -> Response:
    await limit(ctx, "write", user.user_id)
    expected = parse_if_match(if_match)
    if expected is None:
        raise ApiError(428, "precondition_required", 'Send If-Match with the version you edited (e.g. "v3")')
    sid = parse_uuid(system_id, "system")
    result = _validated(body.yaml)
    async with ctx.db.write() as conn:
        sys_row = await conn.fetchrow(
            """UPDATE systems SET latest_version = latest_version + 1, updated_at = now(),
                      name = COALESCE($4, name)
               WHERE id = $1 AND owner_id = $2 AND deleted_at IS NULL AND latest_version = $3
               RETURNING *""",
            sid,
            uuid.UUID(user.user_id),
            expected,
            body.name.strip()[:80] if body.name else None,
        )
        if sys_row is None:
            current = await conn.fetchval(
                "SELECT latest_version FROM systems WHERE id = $1 AND owner_id = $2 AND deleted_at IS NULL",
                sid,
                uuid.UUID(user.user_id),
            )
            if current is None:
                raise ApiError(404, "not_found", "No such system")
            raise ApiError(
                412,
                "version_conflict",
                "Someone saved a newer version first",
                f"You edited v{expected}; the latest is v{current}. Reload and re-apply your change.",
                latest_version=current,
            )
        ver_row = await conn.fetchrow(
            """INSERT INTO system_versions (system_id, version, spec_yaml, spec_json, spec_hash, checks)
               VALUES ($1, $2, $3, $4, $5, $6) RETURNING *""",
            sid,
            sys_row["latest_version"],
            body.yaml,
            result["system"],
            result["spec_hash"],
            _checks_doc(result),
        )
        await log_activity(conn, user.user_id, "system.updated", {"version": sys_row["latest_version"]})
        lsn = await ctx.db.current_lsn(conn)
    return JSON(
        _system_json(sys_row, ver_row),
        headers={"etag": etag_for_version(sys_row["latest_version"]), "x-kessler-lsn": lsn},
    )


@router.delete("/systems/{system_id}", status_code=204)
async def delete_system(system_id: str, user: User, ctx: Ctx) -> Response:
    await limit(ctx, "write", user.user_id)
    async with ctx.db.write() as conn:
        done = await conn.fetchval(
            """UPDATE systems SET deleted_at = now() WHERE id = $1 AND owner_id = $2 AND deleted_at IS NULL
               RETURNING 1""",
            parse_uuid(system_id, "system"),
            uuid.UUID(user.user_id),
        )
    if not done:
        raise ApiError(404, "not_found", "No such system")
    return Response(status_code=204)
