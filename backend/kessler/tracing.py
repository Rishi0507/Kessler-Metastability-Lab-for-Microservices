from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .logs import get_logger, trace_id_var

log = get_logger("kessler.tracing")
_tracer = trace.get_tracer("kessler")


def setup_tracing(service: str, endpoint: str | None) -> None:
    global _tracer
    provider = TracerProvider(resource=Resource.create({"service.name": f"kessler-{service}"}))
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
            )
        except Exception as exc:  # pragma: no cover
            log.warning("OTLP exporter unavailable", extra={"error": str(exc)})
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("kessler")


@contextmanager
def span(name: str, carrier: dict[str, str] | None = None, **attrs: Any) -> Iterator[trace.Span]:
    ctx = propagate.extract(carrier) if carrier else None
    with _tracer.start_as_current_span(name, context=ctx) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v)
        sc = s.get_span_context()
        token = trace_id_var.set(format(sc.trace_id, "032x")) if sc.is_valid else None
        try:
            yield s
        finally:
            if token is not None:
                trace_id_var.reset(token)


def inject() -> dict[str, str]:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier
