"""Process-level OTel instrumentation and local span helpers."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext, suppress
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from weakref import WeakKeyDictionary

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
    SpanProcessor,
)
from opentelemetry.trace import Span, SpanKind
from pydantic import ConfigDict, JsonValue, TypeAdapter

from kensa._serialization import jsonable
from kensa.runtime import OperationKind, current_runtime
from kensa.traces import write_trace_manifest

GenAIOperationName = Literal["chat", "embeddings", "generate_content", "text_completion"]
_LOGGER = logging.getLogger(__name__)
_MISSING_TOOL_PAYLOAD = object()
_DEFAULT_OTLP_TIMEOUT_S = 2.0
_REGISTERED_PROCESSORS: WeakKeyDictionary[Any, set[tuple[str, str]]] = WeakKeyDictionary()
_REGISTERED_PROCESSORS_LOCK = Lock()
_JSON_VALUE_ADAPTER = TypeAdapter(
    JsonValue,
    config=ConfigDict(strict=True, allow_inf_nan=False),
)


class _ToolCallCapture:
    def __init__(self, span: Span, *, result_recorded: bool) -> None:
        self._span = span
        self._result_recorded = result_recorded

    def set_result(self, result: Any) -> None:
        """Record one result produced inside the tool context."""
        if self._result_recorded:
            raise RuntimeError("record_tool_call result is already recorded")
        encoded = _encode_tool_payload(
            result,
            boundary="record_tool_call().set_result(...)",
        )
        self._span.set_attribute("kensa.tool.result", encoded)
        self._result_recorded = True


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


class _ReportingSpanExporter(SpanExporter):
    def __init__(self, exporter: SpanExporter) -> None:
        self._exporter = exporter

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self._exporter.export(spans)
        except Exception:
            _LOGGER.exception("OTLP HTTP span export failed")
            return SpanExportResult.FAILURE
        if result is not SpanExportResult.SUCCESS:
            _LOGGER.error("OTLP HTTP span export failed")
        return result

    def shutdown(self) -> None:
        try:
            self._exporter.shutdown()
        except Exception:
            _LOGGER.exception("OTLP HTTP exporter shutdown failed")

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        try:
            return self._exporter.force_flush(timeout_millis)
        except Exception:
            _LOGGER.exception("OTLP HTTP exporter flush failed")
            return False


def instrument(
    trace_dir: str | Path | None = None,
    *,
    run_id: str | None = None,
    service_name: str | None = None,
    otlp_endpoint: str | None = None,
    otlp_headers: Mapping[str, str] | None = None,
    otlp_timeout_s: float | None = None,
) -> None:
    """Attach local JSONL and optional OTLP HTTP exporters for this process.

    OTLP export is enabled by ``otlp_endpoint`` or standard OpenTelemetry OTLP
    endpoint environment variables. When OTLP is enabled without an explicit
    local directory, JSONL spans are retained under ``.kensa/traces``. The
    default OTLP request timeout is two seconds unless a standard OpenTelemetry
    timeout environment variable or ``otlp_timeout_s`` supplies another value;
    the Python OTLP exporter interprets each timeout in seconds.
    """

    configured = trace_dir if trace_dir is not None else os.environ.get("KENSA_TRACE_DIR")
    otlp_enabled = _otlp_http_enabled(otlp_endpoint)
    if not configured and not otlp_enabled:
        return
    configured = configured or Path(".kensa/traces")
    configured_run_id = run_id or os.environ.get("KENSA_TRACE_RUN_ID")
    configured_service_name = service_name or os.environ.get("KENSA_SERVICE_NAME")
    resolved = Path(configured)
    if configured_run_id:
        resolved = resolved / "runs" / configured_run_id
    output_path = resolved / "spans.jsonl"
    provider = trace.get_tracer_provider()
    processor_factories: list[tuple[str, str, Callable[[], SpanProcessor | None]]] = [
        (
            "jsonl",
            str(output_path.resolve()),
            lambda: _build_jsonl_span_processor(
                output_path,
                run_id=configured_run_id,
                service_name=configured_service_name,
            ),
        )
    ]
    if otlp_enabled:
        resolved_otlp_timeout = _resolved_otlp_timeout(otlp_timeout_s)
        otlp_key = _otlp_registration_key(
            endpoint=otlp_endpoint,
            headers=otlp_headers,
            timeout_s=resolved_otlp_timeout,
        )
        processor_factories.append(
            (
                "otlp-http",
                otlp_key,
                lambda: _build_otlp_http_processor(
                    endpoint=otlp_endpoint,
                    headers=otlp_headers,
                    timeout_s=resolved_otlp_timeout,
                ),
            )
        )
    if _add_span_processors_once(provider, processor_factories):
        return
    new_provider = TracerProvider()
    with suppress(Exception):
        trace.set_tracer_provider(new_provider)
    active_provider = trace.get_tracer_provider()
    if _add_span_processors_once(active_provider, processor_factories):
        return
    _LOGGER.error(
        "OpenTelemetry tracer provider does not support span processors; "
        "local JSONL and OTLP HTTP exporters were not installed"
    )


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


def _add_span_processors_once(
    provider: Any,
    processor_factories: Sequence[tuple[str, str, Callable[[], SpanProcessor | None]]],
) -> bool:
    add_span_processor = getattr(provider, "add_span_processor", None)
    if not callable(add_span_processor):
        return False
    for kind, configuration, factory in processor_factories:
        key = (kind, configuration)
        with _REGISTERED_PROCESSORS_LOCK:
            registered = _REGISTERED_PROCESSORS.setdefault(provider, set())
            if key in registered:
                continue
            registered.add(key)
        try:
            processor = factory()
            if processor is None:
                with _REGISTERED_PROCESSORS_LOCK:
                    registered.discard(key)
                continue
            add_span_processor(processor)
        except Exception:
            with _REGISTERED_PROCESSORS_LOCK:
                registered.discard(key)
            raise
    return True


def _build_jsonl_span_processor(
    output_path: Path,
    *,
    run_id: str | None,
    service_name: str | None,
) -> SpanProcessor | None:
    try:
        exporter = JSONLSpanExporter(
            output_path,
            run_id=run_id,
            service_name=service_name,
        )
    except OSError:
        _LOGGER.exception(
            "Local JSONL span exporter could not be initialized; local capture disabled"
        )
        return None
    return SimpleSpanProcessor(exporter)


def _build_otlp_http_processor(
    *,
    endpoint: str | None,
    headers: Mapping[str, str] | None,
    timeout_s: float | None,
) -> SpanProcessor | None:
    exporter: SpanExporter | None = None
    try:
        exporter = _otlp_span_exporter_class()(
            endpoint=endpoint,
            headers=dict(headers) if headers is not None else None,
            timeout=timeout_s,
        )
        return BatchSpanProcessor(_ReportingSpanExporter(exporter))
    except Exception:
        if exporter is not None:
            with suppress(Exception):
                exporter.shutdown()
        _LOGGER.exception("OTLP HTTP exporter configuration is invalid; OTLP export disabled")
        return None


def _otlp_span_exporter_class() -> Callable[..., SpanExporter]:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter


def _otlp_http_enabled(endpoint: str | None) -> bool:
    if endpoint:
        return True
    exporter_selection = os.environ.get("OTEL_TRACES_EXPORTER", "").strip()
    selected_exporters = {
        entry.strip().lower() for entry in exporter_selection.split(",") if entry.strip()
    }
    if selected_exporters and "otlp" not in selected_exporters:
        return False
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    )


def _otlp_registration_key(
    *,
    endpoint: str | None,
    headers: Mapping[str, str] | None,
    timeout_s: float | None,
) -> str:
    payload = {
        "endpoint": endpoint,
        "environment": sorted(
            (name, value)
            for name, value in os.environ.items()
            if name.startswith("OTEL_EXPORTER_OTLP")
        ),
        "headers": sorted((headers or {}).items()),
        "timeout_s": timeout_s,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _resolved_otlp_timeout(timeout_s: float | None) -> float | None:
    if timeout_s is not None:
        return timeout_s
    if os.environ.get("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_TIMEOUT"
    ):
        return None
    return _DEFAULT_OTLP_TIMEOUT_S


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
) -> Iterator[Span]:
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
        ) as span,
    ):
        yield span


@contextmanager
def record_tool_call(
    name: str,
    *,
    arguments: Any = _MISSING_TOOL_PAYLOAD,
    result: Any = _MISSING_TOOL_PAYLOAD,
    **attributes: Any,
) -> Iterator[_ToolCallCapture]:
    encoded_arguments = (
        None
        if arguments is _MISSING_TOOL_PAYLOAD
        else _encode_tool_payload(
            arguments,
            boundary="record_tool_call(arguments=...)",
        )
    )
    encoded_result = (
        None
        if result is _MISSING_TOOL_PAYLOAD
        else _encode_tool_payload(
            result,
            boundary="record_tool_call(result=...)",
        )
    )
    operation_attributes = _flatten_attributes(attributes)
    attrs = dict(operation_attributes)
    attrs.update(
        {
            "kensa.span.kind": "tool",
            "kensa.tool.name": name,
        }
    )
    if encoded_arguments is not None:
        attrs["kensa.tool.args"] = encoded_arguments
        operation_attributes["arguments"] = json.loads(encoded_arguments)
    if encoded_result is not None:
        attrs["kensa.tool.result"] = encoded_result
        operation_attributes["result"] = json.loads(encoded_result)
    with _record_span(
        name,
        span_attributes=attrs,
        operation_attributes=operation_attributes,
        operation_kind="tool",
    ) as span:
        yield _ToolCallCapture(
            span,
            result_recorded=result is not _MISSING_TOOL_PAYLOAD,
        )


def _encode_tool_payload(value: Any, *, boundary: str) -> str:
    try:
        snapshot = _JSON_VALUE_ADAPTER.validate_python(value)
        return json.dumps(
            snapshot,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{boundary} must be a strict JSON value: {exc}") from exc


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
