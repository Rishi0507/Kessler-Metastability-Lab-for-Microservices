from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import orjson
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .. import keys
from ..config import get_settings
from ..heartbeat import cancel, heartbeat_loop, supervise
from ..logs import get_logger, setup_logging
from ..metrics import WS_CONNECTIONS, WS_MESSAGES, render
from ..ratelimit import RateLimiter
from ..redisio import create_redis
from ..security import verify_stream_token
from ..tracing import setup_tracing
from .hub import Hub, Message, Subscriber

log = get_logger("kessler.gateway")
MAX_SUBS_PER_SOCKET = 32


class Gateway:
    def __init__(self) -> None:
        self.s = get_settings()
        self.redis = create_redis(self.s.redis_url)
        self.pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        self.hub = Hub()
        self.limiter = RateLimiter(self.redis)
        self._sub_lock = asyncio.Lock()
        self.connections = 0
        self.sent = 0

    async def subscribe(self, topic: str) -> None:
        async with self._sub_lock:
            await self.pubsub.subscribe(keys.channel(topic))

    async def unsubscribe(self, topic: str) -> None:
        async with self._sub_lock:
            await self.pubsub.unsubscribe(keys.channel(topic))

    async def reader(self) -> None:
        while True:
            if not self.pubsub.subscribed:
                await asyncio.sleep(0.05)
                continue
            try:
                msg = await self.pubsub.get_message(timeout=1.0)
            except Exception as exc:
                log.warning("pubsub read failed", extra={"error": str(exc)})
                await asyncio.sleep(0.5)
                continue
            if msg and msg.get("type") == "message":
                try:
                    self.hub.publish(Message.from_envelope(msg["data"]))
                except Exception:
                    log.exception("bad envelope")

    def stats(self) -> dict[str, Any]:
        return {
            "connections": self.connections,
            "fanout_p99_ms": self.hub.fanout_p99_ms(),
            "sent": self.sent,
            **self.hub.stats(),
        }


def stream_key(topic: str) -> str | None:
    kind, _, ident = topic.partition(":")
    if kind == "analysis":
        return keys.progress_stream(ident)
    if kind == "run":
        return keys.run_ticks(ident)
    return None


async def sender(ws: WebSocket, sub: Subscriber, gw: Gateway) -> None:
    while not sub.closed:
        await sub.wake.wait()
        if sub.slow:
            await ws.close(code=1013, reason="client too slow; reconnect and resume")
            return
        for m in sub.drain():
            await ws.send_text(m.raw.decode())
            WS_MESSAGES.labels("out", m.kind or "event").inc()
            gw.sent += 1


async def control(ws: WebSocket, sub: Subscriber, gw: Gateway, data: dict[str, Any]) -> None:
    topic = str(data.get("topic", ""))
    if sub.perms.get(topic) != "rw" or not topic.startswith("run:"):
        await ws.send_text(orjson.dumps({"type": "error", "req": data.get("req"), "error": "not allowed"}).decode())
        return
    d = await gw.limiter.check("control", sub.user_id)
    if not d.allowed:
        await ws.send_text(
            orjson.dumps(
                {"type": "error", "req": data.get("req"), "error": "rate limited", "retry_after_s": d.retry_after_s}
            ).decode()
        )
        return
    cmd = data.get("cmd")
    if not isinstance(cmd, dict):
        await ws.send_text(
            orjson.dumps({"type": "error", "req": data.get("req"), "error": "cmd must be an object"}).decode()
        )
        return
    await gw.redis.xadd(
        keys.run_ctl(topic.split(":", 1)[1]),
        {
            b"cmd": orjson.dumps(cmd),
            b"req": str(data.get("req") or secrets.token_hex(4)).encode(),
            b"user": sub.user_id.encode(),
            b"ts": str(time.time()).encode(),
        },
        maxlen=1000,
        approximate=True,
    )


async def subscribe(ws: WebSocket, sub: Subscriber, gw: Gateway, data: dict[str, Any]) -> None:
    topic = str(data.get("topic", ""))
    claims = verify_stream_token(gw.s, str(data.get("token", "")))
    skey = stream_key(topic)
    if not claims or claims["topic"] != topic or claims["user_id"] != sub.user_id or skey is None:
        await ws.send_text(orjson.dumps({"type": "error", "topic": topic, "error": "forbidden"}).decode())
        return
    if topic not in sub.perms and len(sub.perms) >= MAX_SUBS_PER_SOCKET:
        await ws.send_text(orjson.dumps({"type": "error", "topic": topic, "error": "too many subscriptions"}).decode())
        return
    sub.perms[topic] = claims["perms"]
    sub.backfilling[topic] = []
    if gw.hub.add(sub, topic):
        await gw.subscribe(topic)
    since = str(data.get("since") or "0-0")
    try:
        entries = await gw.redis.xrange(skey, min=f"({since}" if since != "0-0" else "-", max="+", count=5000)
    except Exception:
        entries = await gw.redis.xrange(skey, min="-", max="+", count=5000)
    buffered = sub.backfilling.pop(topic, [])
    for eid, fields in entries:
        sid = eid.decode()
        data_bytes = fields.get(b"e", b"{}")
        env = b'{"topic":"' + topic.encode() + b'","id":"' + sid.encode() + b'","event":' + data_bytes + b"}"
        sub.offer(Message.from_envelope(env))
    for m in buffered:
        sub.offer(m)
    await ws.send_text(orjson.dumps({"type": "subscribed", "topic": topic, "backfilled": len(entries)}).decode())


async def websocket_endpoint(ws: WebSocket) -> None:
    gw: Gateway = ws.app.state.gw
    ticket = ws.query_params.get("ticket", "")
    user = await gw.redis.getdel(keys.ticket(ticket)) if ticket else None
    if not user:
        await ws.close(code=4401, reason="missing or expired ticket")
        return
    await ws.accept()
    sub = Subscriber(user_id=user.decode())
    gw.connections += 1
    WS_CONNECTIONS.inc()
    send_task = asyncio.create_task(sender(ws, sub, gw))
    try:
        while True:
            text = await ws.receive_text()
            if len(text) > 16_384:
                await ws.close(code=1009, reason="message too big")
                break
            WS_MESSAGES.labels("in", "message").inc()
            try:
                data = orjson.loads(text)
            except orjson.JSONDecodeError:
                await ws.send_text('{"type":"error","error":"invalid json"}')
                continue
            op = data.get("op")
            if op == "sub":
                await subscribe(ws, sub, gw, data)
            elif op == "unsub":
                topic = str(data.get("topic", ""))
                sub.perms.pop(topic, None)
                if gw.hub.remove(sub, topic):
                    await gw.unsubscribe(topic)
            elif op == "ctl":
                await control(ws, sub, gw, data)
            elif op == "ping":
                await ws.send_text(orjson.dumps({"type": "pong", "ts": time.time()}).decode())
            else:
                await ws.send_text('{"type":"error","error":"unknown op"}')
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        sub.closed = True
        sub.wake.set()
        for topic in list(sub.perms):
            if gw.hub.remove(sub, topic):
                with contextlib.suppress(Exception):
                    await gw.unsubscribe(topic)
        await cancel(send_task)
        gw.connections -= 1
        WS_CONNECTIONS.dec()


async def healthz(_: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def readyz(request: Request) -> Response:
    gw: Gateway = request.app.state.gw
    try:
        ok = bool(await asyncio.wait_for(gw.redis.ping(), timeout=1.0))
    except Exception:
        ok = False
    return JSONResponse({"status": "ready" if ok else "degraded", "redis": ok}, status_code=200 if ok else 503)


async def metrics(_: Request) -> Response:
    body, ctype = render()
    return Response(body, media_type=ctype)


def create_app() -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        s = get_settings()
        setup_logging("gateway", s.log_level)
        setup_tracing("gateway", s.otlp_endpoint)
        gw = Gateway()
        app.state.gw = gw
        reader = asyncio.create_task(supervise(gw.reader, "pubsub reader", log))
        hb = asyncio.create_task(heartbeat_loop(gw.redis, "gateway", s.instance, gw.stats))
        log.info("gateway ready", extra={"port": s.port})
        try:
            yield
        finally:
            await cancel(reader, hb)
            with contextlib.suppress(Exception):
                await gw.pubsub.aclose()
            await gw.redis.aclose()

    return Starlette(
        routes=[
            WebSocketRoute("/ws", websocket_endpoint),
            Route("/healthz", healthz),
            Route("/readyz", readyz),
            Route("/metrics", metrics),
        ],
        lifespan=lifespan,
    )
