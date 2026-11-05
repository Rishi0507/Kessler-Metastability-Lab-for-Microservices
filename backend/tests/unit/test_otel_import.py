import json

import pytest

from kessler.spec import compile_text
from kessler.spec.otel import TraceImportError, import_otlp

MS = 1_000_000


def _span(trace, sid, parent, kind, start_ms, dur_ms, error=False, attrs=None):
    s = {
        "traceId": trace,
        "spanId": sid,
        "parentSpanId": parent,
        "name": sid,
        "kind": kind,
        "startTimeUnixNano": str(int(start_ms * MS)),
        "endTimeUnixNano": str(int((start_ms + dur_ms) * MS)),
        "status": {"code": 2 if error else 0},
    }
    if attrs:
        s["attributes"] = [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()]
    return s


def _resource(service, spans, instance="i-1"):
    attrs = [
        {"key": "service.name", "value": {"stringValue": service}},
        {"key": "service.instance.id", "value": {"stringValue": instance}},
    ]
    return {"resource": {"attributes": attrs}, "scopeSpans": [{"spans": spans}]}


def _traces(n=100, retry_every=10):
    gateway, checkout, inventory = [], [], []
    for i in range(n):
        t = f"t{i:04d}"
        start = i * 100.0
        gateway.append(_span(t, "g", "", "SPAN_KIND_SERVER", start, 40))
        gateway.append(_span(t, "gc", "g", "SPAN_KIND_CLIENT", start + 2, 36))
        checkout.append(_span(t, "c", "gc", "SPAN_KIND_SERVER", start + 3, 34))
        checkout.append(_span(t, "ci", "c", "SPAN_KIND_CLIENT", start + 5, 10))
        inventory.append(_span(t, "is", "ci", "SPAN_KIND_SERVER", start + 6, 8))
        db_calls = 2 if i % retry_every == 0 else 1
        for a in range(db_calls):
            err = a < db_calls - 1
            checkout.append(_span(t, f"cd{a}", "c", 3, start + 16 + a * 6, 5, err, {"db.system": "postgresql"}))
    resources = [_resource("gateway", gateway), _resource("checkout", checkout), _resource("Inventory API", inventory)]
    return {"resourceSpans": resources}


def test_imports_services_calls_and_traffic_from_otlp_json():
    yaml_text, notes = import_otlp(json.dumps(_traces()), name="Shop")
    result = compile_text(yaml_text)
    assert not result.errors, result.errors
    spec = result.spec
    assert set(spec.services) == {"gateway", "checkout", "inventory-api", "postgresql"}
    assert spec.traffic.entry == "gateway"
    assert spec.traffic.rps == pytest.approx(10, rel=0.05)
    calls = {c.to: c for c in spec.services["checkout"].calls}
    assert set(calls) == {"inventory-api", "postgresql"}
    assert calls["postgresql"].retry is not None and calls["postgresql"].retry.attempts == 2
    assert calls["inventory-api"].retry is None
    assert notes == []


def test_latency_is_own_work_without_downstream_time():
    yaml_text, _ = import_otlp(json.dumps(_traces()))
    spec = compile_text(yaml_text).spec
    assert spec.services["gateway"].latency.p50 == pytest.approx(4, abs=0.5)
    assert spec.services["inventory-api"].latency.p50 == pytest.approx(8, abs=0.5)


def test_accepts_json_lines_from_the_collector_file_exporter():
    doc = _traces(20)
    lines = "\n".join(json.dumps({"resourceSpans": [rs]}) for rs in doc["resourceSpans"])
    yaml_text, _ = import_otlp(lines)
    assert not compile_text(yaml_text).errors


def test_rejects_input_without_spans():
    with pytest.raises(TraceImportError):
        import_otlp(json.dumps({"resourceSpans": []}))
    with pytest.raises(TraceImportError):
        import_otlp("not json\nstill not json")
