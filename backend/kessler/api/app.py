from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .. import errors
from ..config import Settings, get_settings
from ..db import Database
from ..engine import get_engine
from ..heartbeat import cancel, heartbeat_loop
from ..logs import get_logger, setup_logging
from ..metrics import HTTP_INFLIGHT
from ..ratelimit import RateLimiter
from ..redisio import create_redis
from ..tracing import setup_tracing
from .common import JSON
from .context import AppContext
from .middleware import RequestContextMiddleware
from .routes import analyses, auth, platform, runs, systems

log = get_logger("kessler.api")


async def _replica_lag_loop(db: Database) -> None:
    while True:
        await db.refresh_replica_lag()
        await asyncio.sleep(5)


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging("api", s.log_level)
        setup_tracing("api", s.otlp_endpoint)
        engine = get_engine()
        db = Database(s, f"kessler-api-{s.port}")
        await db.connect()
        redis = create_redis(s.redis_url)
        app.state.ctx = AppContext(settings=s, db=db, redis=redis, limiter=RateLimiter(redis))

        def stats() -> dict[str, object]:
            return {"port": s.port, "inflight": HTTP_INFLIGHT._value.get(), "engine": engine.version}

        hb = asyncio.create_task(heartbeat_loop(redis, "api", s.instance, stats))
        lag = asyncio.create_task(_replica_lag_loop(db))
        log.info("api ready", extra={"port": s.port, "engine": engine.version, "replica": db.replica is not None})
        try:
            yield
        finally:
            await cancel(hb, lag)
            await redis.aclose()
            await db.close()

    app = FastAPI(
        title="Kessler API",
        version="0.1.0",
        description="Find the load and fault combinations that tip a system into metastable failure.",
        lifespan=lifespan,
        default_response_class=JSON,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    errors.install(app)
    app.add_middleware(RequestContextMiddleware)
    for module in (platform, auth, systems, analyses, runs):
        app.include_router(module.router)
    return app
