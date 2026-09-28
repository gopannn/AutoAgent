from __future__ import annotations

import builtins
import json

import pytest

from ctd.tracing import (
    InMemoryTraceSink,
    JsonLineTraceSink,
    OpenTelemetryTraceSink,
    Tracer,
    sanitize_trace_attributes,
)


def test_tracer_records_nested_parentage_and_error_status():
    sink = InMemoryTraceSink()
    tracer = Tracer(sink)

    with tracer.span("outer", attributes={"tenant": "acme"}) as outer:
        with tracer.span("inner") as inner:
            assert inner.trace_id == outer.trace_id
            assert inner.parent_span_id == outer.span_id
        with pytest.raises(RuntimeError):
            with tracer.span("failing"):
                raise RuntimeError("boom")

    spans = sink.spans()
    assert [span.name for span in spans] == ["inner", "failing", "outer"]
    assert spans[0].status == "ok"
    assert spans[1].status == "error"
    assert spans[2].attributes["tenant"] == "acme"


def test_jsonl_trace_sink_writes_completed_spans_and_sanitizes_sensitive_attributes(tmp_path):
    path = tmp_path / "traces.jsonl"
    sink = JsonLineTraceSink(path)
    tracer = Tracer(sink)

    attrs = sanitize_trace_attributes(
        {
            "tenant": "acme",
            "authorization": "Bearer secret",
            "api_key": "abc",
            "jwt_token": "xyz",
            "count": 3,
        }
    )
    with tracer.span("request", attributes=attrs):
        pass

    payload = json.loads(path.read_text().strip())
    assert payload["name"] == "request"
    assert payload["attributes"]["tenant"] == "acme"
    assert payload["attributes"]["count"] == 3
    assert payload["attributes"]["authorization"] == "[REDACTED]"
    assert payload["attributes"]["api_key"] == "[REDACTED]"
    assert payload["attributes"]["jwt_token"] == "[REDACTED]"


class FakeOtelSpan:
    def __init__(self, name, records):
        self.name = name
        self.records = records
        self.attributes = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.records.append((self.name, dict(self.attributes)))
        return False


class FakeOtelTracer:
    def __init__(self):
        self.records = []

    def start_as_current_span(self, name):
        return FakeOtelSpan(name, self.records)


def test_opentelemetry_sink_forwards_span_name_and_safe_attributes():
    otel = FakeOtelTracer()
    sink = OpenTelemetryTraceSink(tracer=otel)
    tracer = Tracer(sink)

    with tracer.span("ctd.resolve", attributes={"tenant": "acme", "count": 2}):
        pass

    assert otel.records[0][0] == "ctd.resolve"
    attrs = otel.records[0][1]
    assert attrs["ctd.tenant"] == "acme"
    assert attrs["ctd.count"] == 2
    assert attrs["ctd.status"] == "ok"


def test_opentelemetry_sink_fails_explicitly_when_dependency_is_unavailable(monkeypatch):
    original_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError("blocked")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(RuntimeError, match="otel"):
        OpenTelemetryTraceSink()
