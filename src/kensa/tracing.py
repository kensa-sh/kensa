"""Process-level OTel instrumentation and local span helpers."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import SpanKind

from kensa._serialization import jsonable
from kensa.runtime import OperationKind, current_runtime
from kensa.traces import write_trace_manifest

GenAIOperationName = Literal["chat", "embeddings", "generate_content", "text_completion"]


class JSONLSpanExporter(SpanExporter):
    """Export finished OpenTelemetry spans as JSON lines."""

    def __init__(
        self,
        output_path: Path | str,
        *,
        run_id: str | None = None,
        service_name: str | None = None,
        source: str = "local-jsonl",
        manifest_update_interval: int = 100,
    ) -> None:
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.service_name = service_name
        self.source = source
        self.manifest_update_interval = max(1, manifest_update_interval)
        self._span_count = 0
        self._trace_ids: set[str] = set()
        self._export_batches = 0
        self._lock = Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [span_to_dict(span) for span in spans]
        with self._lock:
            with self.output_path.open("a") as handle:
                for row in rows:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
            for row in rows:
                if row.get("trace_id"):
                    self._trace_ids.add(str(row["trace_id"]))
            self._span_count += len(rows)
            self._export_batches += 1
            if self.run_id and (
                self._export_batches == 1
                or self._export_batches % self.manifest_update_interval == 0
            ):
                self._write_manifest_locked()
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        with self._lock:
            if self.run_id:
                self._write_manifest_locked()
        return

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        with self._lock:
            if self.run_id:
                self._write_manifest_locked()
        return True

    def _write_manifest_locked(self) -> None:
        write_trace_manifest(
            self.output_path.parent,
            run_id=str(self.run_id),
            source=self.source,
            service_name=self.service_name,
            span_count=self._span_count,
            trace_count=len(self._trace_ids),
        )


def instrument(
    trace_dir: str | Path | None = None,
    *,
    run_id: str | None = None,
    service_name: str | None = None,
) -> None:
    """Attach a JSONL OTel exporter for the current process.

    The function is intentionally explicit and process-scoped. It no-ops unless
    ``trace_dir`` or ``KENSA_TRACE_DIR`` is set, so users can leave it in app
    startup without affecting normal runs.
    """

    configured = trace_dir if trace_dir is not None else os.environ.get("KENSA_TRACE_DIR")
    if not configured:
        return
    configured_run_id = run_id or os.environ.get("KENSA_TRACE_RUN_ID")
    configured_service_name = service_name or os.environ.get("KENSA_SERVICE_NAME")
    resolved = Path(configured)
    if configured_run_id:
        resolved = resolved / "runs" / configured_run_id
    output_path = resolved / "spans.jsonl"
    provider = trace.get_tracer_provider()
    exporter = JSONLSpanExporter(
        output_path,
        run_id=configured_run_id,
        service_name=configured_service_name,
    )
    if _add_jsonl_processor(provider, exporter):
        return
    new_provider = TracerProvider()
    new_provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        trace.set_tracer_provider(new_provider)
    except Exception:
        _add_jsonl_processor(trace.get_tracer_provider(), exporter)


def span_to_dict(span: ReadableSpan) -> dict[str, Any]:
    context = span.get_span_context()
    if context is None:
        trace_id = None
        span_id = None
    else:
        trace_id = f"{context.trace_id:032x}"
        span_id = f"{context.span_id:016x}"
    parent = span.parent
    status_code = getattr(getattr(span, "status", None), "status_code", None)
    status_name = getattr(status_code, "name", "OK").lower()
    status_message = getattr(getattr(span, "status", None), "description", None)
    attributes = {str(key): jsonable(value) for key, value in dict(span.attributes or {}).items()}
    return {
        "name": span.name,
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": f"{parent.span_id:016x}" if parent else None,
        "start_time_unix_nano": span.start_time,
        "end_time_unix_nano": span.end_time,
        "status": "error" if status_name == "error" else "ok",
        "status_message": status_message,
        "attributes": attributes,
        "resource_attributes": _resource_attributes(span),
        "instrumentation_scope": _instrumentation_scope(span),
        "events": _span_events(span),
        "links": _span_links(span),
        "trace_state": str(context.trace_state) if context is not None else None,
    }


def _add_jsonl_processor(provider: Any, exporter: JSONLSpanExporter | Path) -> bool:
    add_span_processor = getattr(provider, "add_span_processor", None)
    if not callable(add_span_processor):
        return False
    if isinstance(exporter, Path):
        exporter = JSONLSpanExporter(exporter)
    add_span_processor(SimpleSpanProcessor(exporter))
    return True


def _resource_attributes(span: ReadableSpan) -> dict[str, Any]:
    resource = getattr(span, "resource", None)
    attributes = getattr(resource, "attributes", None)
    return {str(key): jsonable(value) for key, value in dict(attributes or {}).items()}


def _instrumentation_scope(span: ReadableSpan) -> dict[str, Any]:
    scope = getattr(span, "instrumentation_scope", None) or getattr(
        span,
        "instrumentation_info",
        None,
    )
    if scope is None:
        return {}
    return {
        "name": getattr(scope, "name", None),
        "version": getattr(scope, "version", None),
        "attributes": jsonable(getattr(scope, "attributes", {}) or {}),
    }


def _span_events(span: ReadableSpan) -> list[dict[str, Any]]:
    return [
        {
            "name": event.name,
            "timestamp": event.timestamp,
            "attributes": {
                str(key): jsonable(value) for key, value in dict(event.attributes or {}).items()
            },
        }
        for event in getattr(span, "events", ())
    ]


def _span_links(span: ReadableSpan) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for link in getattr(span, "links", ()):
        context = link.context
        links.append(
            {
                "trace_id": f"{context.trace_id:032x}",
                "span_id": f"{context.span_id:016x}",
                "attributes": {
                    str(key): jsonable(value) for key, value in dict(link.attributes or {}).items()
                },
            }
        )
    return links


@contextmanager
def record_span(name: str, **attributes: Any) -> Iterator[None]:
    attrs = _flatten_attributes(attributes)
    with _record_span(
        name,
        span_attributes=attrs,
        operation_attributes=attrs,
        operation_kind="span",
    ):
        yield


@contextmanager
def _record_span(
    name: str,
    *,
    span_attributes: dict[str, Any],
    operation_attributes: dict[str, Any],
    operation_kind: OperationKind,
    span_kind: SpanKind = SpanKind.INTERNAL,
) -> Iterator[None]:
    tracer = trace.get_tracer("kensa.app")
    runtime = current_runtime()
    operation = (
        runtime.operation(name, operation_attributes, kind=operation_kind)
        if runtime is not None
        else nullcontext()
    )
    with (
        operation,
        tracer.start_as_current_span(
            name,
            kind=span_kind,
            attributes=span_attributes,
        ),
    ):
        yield


@contextmanager
def record_tool_call(name: str, **attributes: Any) -> Iterator[None]:
    operation_attributes = _flatten_attributes(attributes)
    attrs = {
        "kensa.span.kind": "tool",
        "kensa.tool.name": name,
    }
    attrs.update(operation_attributes)
    with _record_span(
        name,
        span_attributes=attrs,
        operation_attributes=operation_attributes,
        operation_kind="tool",
    ):
        yield


@contextmanager
def record_llm_call(
    name: str = "llm.call",
    *,
    provider: str | None = None,
    model: str | None = None,
    operation_name: GenAIOperationName = "chat",
    span_kind: SpanKind = SpanKind.CLIENT,
    **attributes: Any,
) -> Iterator[None]:
    operation_attributes = _flatten_attributes(attributes)
    attrs = {
        "kensa.span.kind": "llm",
        "gen_ai.operation.name": operation_name,
    }
    if provider is not None:
        attrs["kensa.llm.provider"] = provider
        attrs["gen_ai.provider.name"] = provider
        operation_attributes["provider"] = provider
    if model is not None:
        attrs["kensa.llm.model"] = model
        attrs["gen_ai.request.model"] = model
        operation_attributes["model"] = model
    attrs.update(_flatten_attributes(attributes))
    with _record_span(
        name,
        span_attributes=attrs,
        operation_attributes=operation_attributes,
        operation_kind="llm",
        span_kind=span_kind,
    ):
        yield


def _flatten_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    values = dict(attributes)
    nested = values.pop("attributes", None)
    if isinstance(nested, dict):
        return {**nested, **values}
    if nested is not None:
        values["attributes"] = nested
    return values


__all__ = [
    "JSONLSpanExporter",
    "instrument",
    "record_llm_call",
    "record_span",
    "record_tool_call",
    "span_to_dict",
]
