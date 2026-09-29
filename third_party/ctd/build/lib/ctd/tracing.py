from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field


_SENSITIVE_PARTS = ("authorization", "password", "secret", "token", "api_key", "apikey", "credential")


def sanitize_trace_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        lowered = key.casefold()
        if any(part in lowered for part in _SENSITIVE_PARTS):
            result[key] = "[REDACTED]"
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            result[key] = value
        elif isinstance(value, (list, tuple, set)):
            result[key] = [str(item) for item in value]
        else:
            result[key] = str(value)
    return result


class TraceSpan(BaseModel):
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str
    started_at: datetime
    ended_at: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)
    status: str = "ok"


class TraceSink(Protocol):
    def emit(self, span: TraceSpan) -> None: ...


class InMemoryTraceSink:
    def __init__(self) -> None:
        self._spans: list[TraceSpan] = []
        self._lock = RLock()

    def emit(self, span: TraceSpan) -> None:
        with self._lock:
            self._spans.append(span.model_copy(deep=True))

    def spans(self) -> list[TraceSpan]:
        with self._lock:
            return [span.model_copy(deep=True) for span in self._spans]


class JsonLineTraceSink:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def emit(self, span: TraceSpan) -> None:
        line = json.dumps(span.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class OpenTelemetryTraceSink:
    def __init__(self, *, tracer: Any | None = None, service_name: str = "ctd-engine") -> None:
        if tracer is None:
            try:
                from opentelemetry import trace  # type: ignore
            except ImportError as exc:
                raise RuntimeError("OpenTelemetry tracing requires the 'otel' optional extra") from exc
            tracer = trace.get_tracer(service_name)
        self.tracer = tracer

    def emit(self, span: TraceSpan) -> None:
        with self.tracer.start_as_current_span(span.name) as active:
            for key, value in sanitize_trace_attributes(span.attributes).items():
                active.set_attribute(f"ctd.{key}", value)
            active.set_attribute("ctd.trace_id", span.trace_id)
            active.set_attribute("ctd.span_id", span.span_id)
            if span.parent_span_id:
                active.set_attribute("ctd.parent_span_id", span.parent_span_id)
            active.set_attribute("ctd.status", span.status)


class _ActiveSpan:
    def __init__(
        self,
        *,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        name: str,
        started_at: datetime,
        attributes: dict[str, Any],
    ) -> None:
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.name = name
        self.started_at = started_at
        self.attributes = attributes


_current_span: ContextVar[_ActiveSpan | None] = ContextVar("ctd_current_span", default=None)


class Tracer:
    def __init__(self, sink: TraceSink) -> None:
        self.sink = sink

    @contextmanager
    def span(
        self,
        name: str,
        *,
        attributes: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> Iterator[_ActiveSpan]:
        parent = _current_span.get()
        active = _ActiveSpan(
            trace_id=trace_id or (parent.trace_id if parent else uuid4().hex),
            span_id=uuid4().hex[:16],
            parent_span_id=parent.span_id if parent else None,
            name=name,
            started_at=datetime.now(tz=UTC),
            attributes=sanitize_trace_attributes(attributes),
        )
        token = _current_span.set(active)
        status = "ok"
        try:
            yield active
        except Exception:
            status = "error"
            raise
        finally:
            ended = datetime.now(tz=UTC)
            _current_span.reset(token)
            self.sink.emit(
                TraceSpan(
                    trace_id=active.trace_id,
                    span_id=active.span_id,
                    parent_span_id=active.parent_span_id,
                    name=active.name,
                    started_at=active.started_at,
                    ended_at=ended,
                    attributes=active.attributes,
                    status=status,
                )
            )
