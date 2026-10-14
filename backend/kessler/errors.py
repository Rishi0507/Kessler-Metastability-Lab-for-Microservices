from __future__ import annotations

from typing import Any

import orjson
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from .logs import get_logger, request_id_var

log = get_logger("kessler.errors")


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        title: str,
        detail: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(title)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.headers = headers or {}
        self.extra = extra


def problem(
    status: int, code: str, title: str, detail: str | None = None, headers: dict[str, str] | None = None, **extra: Any
) -> Response:
    body: dict[str, Any] = {
        "type": f"https://kessler.dev/problems/{code}",
        "title": title,
        "status": status,
        "code": code,
    }
    if detail:
        body["detail"] = detail
    rid = request_id_var.get()
    if rid:
        body["request_id"] = rid
    body.update(extra)
    return Response(orjson.dumps(body), status_code=status, media_type="application/problem+json", headers=headers)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> Response:
        return problem(exc.status, exc.code, exc.title, exc.detail, exc.headers, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> Response:
        errors = [
            {"path": ".".join(str(p) for p in e.get("loc", ()) if p != "body"), "message": e.get("msg", "invalid")}
            for e in exc.errors()
        ]
        return problem(422, "validation_failed", "The request is invalid", errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> Response:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return problem(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> Response:
        log.exception("unhandled error", extra={"error": repr(exc)})
        return problem(500, "internal", "Something went wrong on our side")
