from __future__ import annotations

import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..logs import get_logger, request_id_var, user_id_var
from ..metrics import HTTP_INFLIGHT, HTTP_LATENCY, HTTP_REQUESTS
from ..tracing import span

log = get_logger("kessler.access")

_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"x-frame-options", b"DENY"),
]


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode(errors="replace") for k, v in scope.get("headers", [])}
        rid = headers.get("x-request-id") or uuid.uuid4().hex
        rid_token = request_id_var.set(rid)
        user_token = user_id_var.set(None)
        status_holder: dict[str, Any] = {"status": 500}
        start = time.perf_counter()
        HTTP_INFLIGHT.inc()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                hdrs = list(message.get("headers", []))
                existing = {k.lower() for k, _ in hdrs}
                hdrs.append((b"x-request-id", rid.encode()))
                for k, v in _SECURITY_HEADERS:
                    if k not in existing:
                        hdrs.append((k, v))
                if b"cache-control" not in existing:
                    hdrs.append((b"cache-control", b"no-store"))
                message["headers"] = hdrs
            await send(message)

        carrier = {k: v for k, v in headers.items() if k in ("traceparent", "tracestate")}
        try:
            with span(f"{scope['method']} {scope['path']}", carrier=carrier, **{"http.method": scope["method"]}) as sp:
                await self.app(scope, receive, send_wrapper)
                route = scope.get("route")
                if route is not None and getattr(route, "path", None):
                    sp.update_name(f"{scope['method']} {route.path}")
                sp.set_attribute("http.status_code", status_holder["status"])
        finally:
            elapsed = time.perf_counter() - start
            HTTP_INFLIGHT.dec()
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            method = scope["method"]
            status = status_holder["status"]
            HTTP_REQUESTS.labels(template, method, str(status)).inc()
            HTTP_LATENCY.labels(template, method).observe(elapsed)
            if template not in ("/healthz", "/readyz", "/metrics"):
                log.info(
                    "request",
                    extra={
                        "method": method,
                        "path": scope["path"],
                        "route": template,
                        "status": status,
                        "ms": round(elapsed * 1000, 2),
                    },
                )
            request_id_var.reset(rid_token)
            user_id_var.reset(user_token)
