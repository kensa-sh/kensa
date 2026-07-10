"""Trace reservoir and bounded import helpers with mandatory redaction."""

from __future__ import annotations

import json
import re
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from kensa.redact import (
    RedactionResult,
    Redactor,
    _safe_url_netloc,
    assert_safe_manifest,
)

TRACE_MANIFEST_SCHEMA_VERSION = "kensa.trace_manifest.v1"
TRACE_VIEW_SCHEMA_VERSION = "kensa.trace_view.v1"
_SECRET_KEY = re.compile(r"(secret|token|password|api[_-]?key|authorization|credential)", re.I)
_ENDPOINT_PLACEHOLDER = "[redacted]"
_IMPORT_PROVIDERS = frozenset({"json", "jsonl", "otlp", "langfuse", "local-jsonl"})
_TRACE_VIEW_KEYS = (
    "schema_version",
    "id",
    "name",
    "source",
    "started_at_unix_nano",
    "ended_at_unix_nano",
    "duration_ms",
    "status",
    "input",
    "output",
    "attributes",
    "spans",
    "raw",
)
_TRACE_SOURCE_KEYS = (
    "provider",
    "import_run_id",
    "imported_at",
    "source_path",
    "source_url",
    "trace_url",
)
_SPAN_VIEW_KEYS = (
    "id",
    "trace_id",
    "parent_id",
    "name",
    "kind",
    "tool_name",
    "started_at_unix_nano",
    "ended_at_unix_nano",
    "duration_ms",
    "status",
    "status_message",
    "input",
    "output",
    "attributes",
    "events",
    "raw",
)
_JSON_TRACE_RESERVED_KEYS = frozenset(
    {
        "attributes",
        "end_time_unix_nano",
        "ended_at_unix_nano",
        "endTime",
        "events",
        "id",
        "input",
        "inputs",
        "name",
        "observations",
        "output",
        "outputs",
        "spans",
        "start_time_unix_nano",
        "started_at_unix_nano",
        "startTime",
        "status",
        "statusCode",
        "status_code",
        "status_message",
        "timestamp",
        "traceId",
        "trace_id",
        "type",
    }
)
_JSON_SPAN_RESERVED_KEYS = frozenset(
    {
        "attributes",
        "end_time_unix_nano",
        "ended_at_unix_nano",
        "endTime",
        "events",
        "id",
        "instrumentation_scope",
        "input",
        "inputs",
        "kind",
        "links",
        "name",
        "observationId",
        "output",
        "outputs",
        "parentSpanId",
        "parent_id",
        "parent_span_id",
        "resource",
        "resource_attributes",
        "spanId",
        "span_id",
        "start_time_unix_nano",
        "started_at_unix_nano",
        "startTime",
        "status",
        "statusCode",
        "status_code",
        "status_message",
        "traceId",
        "trace_id",
        "trace_state",
        "type",
        "tool_name",
        "toolName",
    }
)
_JSON_SPAN_ROW_ONLY_KEYS = frozenset(
    {
        "parentSpanId",
        "parent_id",
        "parent_span_id",
    }
)


class _JsonTraceRecordContract(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, strict=True)

    @model_validator(mode="before")
    @classmethod
    def validate_trace_record(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("trace record must be a JSON object")
        _required_trace_id(value)
        spans = value.get("spans")
        if spans is not None and (
            not isinstance(spans, list) or not all(isinstance(span, dict) for span in spans)
        ):
            raise ValueError("trace record spans must be a list of span objects")
        return value


class _JsonSpanRowContract(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, strict=True)

    @model_validator(mode="before")
    @classmethod
    def validate_span_row(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("span row must be a JSON object")
        trace_id = _required_span_trace_id(value)
        _required_span_id(value, trace_id=trace_id)
        return value


def _raw_source_manifest() -> dict[str, Any]:
    """Explicit raw-source marker for runtime trial telemetry.

    Runtime trace run directories contain raw payloads. They are never directly
    exposable as evidence; `safe_manifest` always treats them as unsafe, and they
    become evidence only through `kensa import`.
    """

    return {
        "raw_source": True,
        "mandatory": False,
        "value_redaction_applied": False,
        "redaction_available": False,
        "note": (
            "Runtime trial telemetry contains raw payloads. Run kensa import to "
            "produce redacted trace evidence."
        ),
    }


@dataclass(frozen=True)
class TraceManifest:
    run_id: str
    created_at: str
    source: str
    service_name: str | None
    files: list[str]
    span_count: int
    trace_count: int
    redaction: dict[str, Any] = field(default_factory=_raw_source_manifest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TRACE_MANIFEST_SCHEMA_VERSION,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "source": self.source,
            "service_name": self.service_name,
            "files": self.files,
            "span_count": self.span_count,
            "trace_count": self.trace_count,
            "redaction": self.redaction,
        }


@dataclass(frozen=True)
class TraceSource:
    provider: str
    import_run_id: str
    imported_at: str
    source_path: str | None
    source_url: str | None
    trace_url: str | None

    def with_trace_url(self, trace_url: str | None) -> TraceSource:
        return TraceSource(
            provider=self.provider,
            import_run_id=self.import_run_id,
            imported_at=self.imported_at,
            source_path=self.source_path,
            source_url=self.source_url,
            trace_url=safe_endpoint(str(trace_url)) if trace_url is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "import_run_id": self.import_run_id,
            "imported_at": self.imported_at,
            "source_path": self.source_path,
            "source_url": self.source_url,
            "trace_url": self.trace_url,
        }


@dataclass(frozen=True)
class SpanView:
    id: str
    trace_id: str
    parent_id: str | None
    name: str
    kind: str
    tool_name: str | None
    started_at_unix_nano: int | None
    ended_at_unix_nano: int | None
    duration_ms: float
    status: Literal["ok", "error", "unknown"]
    status_message: str | None
    input: Any
    output: Any
    attributes: dict[str, Any]
    events: list[dict[str, Any]]
    raw: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind,
            "tool_name": self.tool_name,
            "started_at_unix_nano": self.started_at_unix_nano,
            "ended_at_unix_nano": self.ended_at_unix_nano,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "status_message": self.status_message,
            "input": self.input,
            "output": self.output,
            "attributes": self.attributes,
            "events": self.events,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class TraceView:
    id: str
    name: str | None
    source: TraceSource
    started_at_unix_nano: int | None
    ended_at_unix_nano: int | None
    duration_ms: float
    status: Literal["ok", "error", "unknown"]
    input: Any
    output: Any
    attributes: dict[str, Any]
    spans: list[SpanView]
    raw: Any
    schema_version: Literal["kensa.trace_view.v1"] = TRACE_VIEW_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "source": self.source.to_dict(),
            "started_at_unix_nano": self.started_at_unix_nano,
            "ended_at_unix_nano": self.ended_at_unix_nano,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "input": self.input,
            "output": self.output,
            "attributes": self.attributes,
            "spans": [span.to_dict() for span in self.spans],
            "raw": self.raw,
        }


@dataclass(frozen=True)
class ImportResult:
    provider: str
    source: str
    out_path: Path
    records_written: int
    bytes_read: int
    span_count: int = 0
    manifest_path: Path | None = None
    redaction: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def trace_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_trace_manifest(
    run_dir: Path | str,
    *,
    run_id: str,
    source: str,
    service_name: str | None,
    span_count: int,
    trace_count: int,
    files: list[str] | None = None,
    created_at: str | None = None,
    redaction: dict[str, Any] | None = None,
) -> TraceManifest:
    manifest = TraceManifest(
        run_id=run_id,
        created_at=created_at or trace_timestamp(),
        source=source,
        service_name=service_name,
        files=files or ["spans.jsonl"],
        span_count=span_count,
        trace_count=trace_count,
        redaction=redaction if redaction is not None else _raw_source_manifest(),
    )
    path = Path(run_dir)
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
    return manifest


def import_trace_source(
    *,
    provider: str,
    source: str | None,
    source_label: str | None = None,
    out: Path | str,
    limit: int,
    max_payload_bytes: int,
    project: str | None = None,
    since: str | None = None,
    endpoint: str | None = None,
) -> ImportResult:
    """Import a bounded local trace export file through mandatory redaction."""

    normalized_provider = _validated_import_arguments(
        provider=provider,
        limit=limit,
        max_payload_bytes=max_payload_bytes,
    )
    if source is None:
        raise ValueError(
            "v1 trace imports read bounded trace export files; pass --source. "
            "Live vendor API pulls with --project/--since are deferred."
        )
    redactor = Redactor()
    source_path = Path(source)
    bytes_read = _bounded_size(source_path, max_payload_bytes)
    data = _decode_source(normalized_provider, source_path)
    return _write_redacted_import(
        parse_provider=normalized_provider,
        provenance_provider=_trace_source_provider(normalized_provider, source_path),
        data=data,
        redactor=redactor,
        stored_source=source_label or source,
        out=out,
        limit=limit,
        max_payload_bytes=max_payload_bytes,
        bytes_read=bytes_read,
        project=project,
        since=since,
        endpoint=endpoint,
    )


def import_trace_records(
    *,
    provider: str,
    payload: Any,
    source_label: str,
    out: Path | str,
    limit: int,
    max_payload_bytes: int,
    project: str | None = None,
    since: str | None = None,
    endpoint: str | None = None,
) -> ImportResult:
    """Import decoded in-memory trace records through mandatory redaction.

    Shared by the file-based and connected import paths. Connected imports use this
    entry point directly so raw fetched payloads never transit disk; the payload
    bound is enforced against the serialized in-memory payload.
    """

    normalized_provider = _validated_import_arguments(
        provider=provider,
        limit=limit,
        max_payload_bytes=max_payload_bytes,
    )
    redactor = Redactor()
    bytes_read = len(json.dumps(payload, sort_keys=True).encode("utf-8"))
    if bytes_read > max_payload_bytes:
        raise ValueError(f"payload exceeds --max-payload-bytes: {bytes_read} > {max_payload_bytes}")
    return _write_redacted_import(
        parse_provider=normalized_provider,
        provenance_provider=normalized_provider,
        data=_decode_payload(normalized_provider, payload),
        redactor=redactor,
        stored_source=source_label,
        out=out,
        limit=limit,
        max_payload_bytes=max_payload_bytes,
        bytes_read=bytes_read,
        project=project,
        since=since,
        endpoint=endpoint,
    )


def _validated_import_arguments(
    *,
    provider: str,
    limit: int,
    max_payload_bytes: int,
) -> str:
    if limit < 1:
        raise ValueError("--limit must be at least 1")
    if max_payload_bytes < 1:
        raise ValueError("--max-payload-bytes must be at least 1")
    normalized_provider = provider.lower()
    if normalized_provider not in _IMPORT_PROVIDERS:
        raise ValueError(f"unsupported trace import provider: {provider}")
    return normalized_provider


def _write_redacted_import(
    *,
    parse_provider: str,
    provenance_provider: str,
    data: Any,
    redactor: Redactor,
    stored_source: str,
    out: Path | str,
    limit: int,
    max_payload_bytes: int,
    bytes_read: int,
    project: str | None,
    since: str | None,
    endpoint: str | None,
) -> ImportResult:
    endpoint_value = safe_endpoint(endpoint) if endpoint else None
    imported_at = trace_timestamp()
    import_run_id = f"import-{imported_at.replace(':', '-')}"
    trace_source = TraceSource(
        provider=provenance_provider,
        import_run_id=import_run_id,
        imported_at=imported_at,
        source_path=_source_path_for_provenance(stored_source),
        source_url=endpoint_value or _source_url_for_provenance(stored_source),
        trace_url=None,
    )
    trace_views = _import_trace_views(
        provider=parse_provider,
        data=data,
        limit=limit,
        trace_source=trace_source,
    )
    span_count = sum(len(trace.spans) for trace in trace_views)
    output = Path(out)
    results: list[RedactionResult] = [
        redactor.redact_trace_view(trace.to_dict()) for trace in trace_views
    ]
    rendered = "".join(json.dumps(result.trace, sort_keys=True) + "\n" for result in results)
    _write_text_atomic(output, rendered)
    redaction_manifest = redactor.manifest()
    manifest_path = _write_import_manifest(
        output,
        provider=provenance_provider,
        source=stored_source,
        project=project,
        since=since,
        endpoint=endpoint_value,
        limit=limit,
        max_payload_bytes=max_payload_bytes,
        records_written=len(trace_views),
        span_count=span_count,
        bytes_read=bytes_read,
        redaction=redaction_manifest,
    )
    warnings = _redaction_warnings(redaction_manifest, endpoint=endpoint)
    return ImportResult(
        provider=provenance_provider,
        source=stored_source,
        out_path=output,
        records_written=len(trace_views),
        bytes_read=bytes_read,
        span_count=span_count,
        manifest_path=manifest_path,
        redaction=redaction_manifest,
        warnings=warnings,
    )


def _redaction_warnings(
    redaction_manifest: dict[str, Any],
    *,
    endpoint: str | None,
) -> list[str]:
    warnings: list[str] = []
    if redaction_manifest.get("secret_keys_redacted"):
        warnings.append("secret-like fields were redacted")
    changed = int(redaction_manifest.get("changed_value_count") or 0)
    if changed:
        warnings.append(f"mandatory value redaction changed {changed} value(s)")
    if endpoint:
        warnings.append("endpoint recorded in import provenance")
    return warnings


def _decode_source(provider: str, source_path: Path) -> Any:
    if provider in {"json", "jsonl", "local-jsonl"}:
        return _read_json_records(source_path)
    return json.loads(source_path.read_text())


def _decode_payload(provider: str, payload: Any) -> Any:
    if provider in {"json", "jsonl", "local-jsonl"}:
        return _coerce_json_records(payload)
    return payload


def _bounded_size(path: Path, max_payload_bytes: int) -> int:
    size = path.stat().st_size
    if size > max_payload_bytes:
        raise ValueError(f"source exceeds --max-payload-bytes: {size} > {max_payload_bytes}")
    return size


def _trace_source_provider(provider: str, source: Path) -> str:
    if provider != "jsonl":
        return provider
    manifest = _local_capture_manifest(source)
    if manifest is not None and manifest.get("source") == "local-jsonl":
        return "local-jsonl"
    return provider


def _local_capture_manifest(source: Path) -> dict[str, Any] | None:
    for candidate in (source.parent / "manifest.json", source.parent.parent / "manifest.json"):
        if not candidate.exists():
            continue
        try:
            payload = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _source_path_for_provenance(source: str) -> str | None:
    if "://" in source or source.endswith(":connected"):
        return None
    return source


def _source_url_for_provenance(source: str) -> str | None:
    return source if "://" in source else None


def _write_text_atomic(output: Path, text: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(text)
        temp_path.replace(output)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _write_import_manifest(
    output: Path,
    *,
    provider: str,
    source: str,
    project: str | None,
    since: str | None,
    endpoint: Any,
    limit: int,
    max_payload_bytes: int,
    records_written: int,
    span_count: int,
    bytes_read: int,
    redaction: dict[str, Any],
) -> Path:
    manifest = {
        "schema_version": "kensa.trace_import_manifest.v1",
        "created_at": trace_timestamp(),
        "provider": provider,
        "source": source,
        "project": project,
        "since": since,
        "endpoint": endpoint,
        "limit": limit,
        "max_payload_bytes": max_payload_bytes,
        "records_written": records_written,
        "trace_count": records_written,
        "span_count": span_count,
        "bytes_read": bytes_read,
        "redaction": redaction,
    }
    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest_path


def safe_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if not parsed.scheme or not parsed.netloc:
        return endpoint
    return urlunsplit(
        (parsed.scheme, _safe_url_netloc(parsed), _safe_endpoint_path(parsed.path), "", "")
    )


def _safe_endpoint_path(path: str) -> str:
    if not path:
        return ""
    parts = [
        _ENDPOINT_PLACEHOLDER if _SECRET_KEY.search(part) else part for part in path.split("/")
    ]
    return "/".join(parts)


def _import_trace_views(
    *,
    provider: str,
    data: Any,
    limit: int,
    trace_source: TraceSource,
) -> list[TraceView]:
    if provider in {"json", "jsonl", "local-jsonl"}:
        return _import_json_trace_views(_dict_items(data), limit, trace_source)
    if provider == "otlp":
        return _import_otlp_trace_views(data, limit, trace_source)
    if provider == "langfuse":
        return _import_langfuse_trace_views(data, limit, trace_source)
    raise ValueError(f"unsupported trace import provider: {provider}")


def _import_json_trace_views(
    records: list[dict[str, Any]],
    limit: int,
    trace_source: TraceSource,
) -> list[TraceView]:
    if _records_are_span_rows(records):
        return _group_json_span_rows(records, limit, trace_source)
    traces: list[TraceView] = []
    for record in records:
        traces.append(_json_record_trace_view(record, trace_source=trace_source))
        if len(traces) >= limit:
            break
    return traces


def _read_json_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text()
    if path.suffix == ".json":
        return _coerce_json_records(json.loads(text))
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _coerce_json_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return _dict_items(data)
    if isinstance(data, dict) and isinstance(data.get("traces"), list):
        return _dict_items(data["traces"])
    return [data] if isinstance(data, dict) else []


def _records_are_span_rows(records: list[dict[str, Any]]) -> bool:
    has_span_rows = any(_json_record_is_span_row(record) for record in records)
    has_trace_rows = any(not _json_record_is_span_row(record) for record in records)
    if has_span_rows and has_trace_rows:
        raise ValueError("JSON import contains mixed trace records and span rows")
    return has_span_rows


def _json_record_is_span_row(record: dict[str, Any]) -> bool:
    if isinstance(record.get("spans"), list):
        return False
    if any(key in record for key in ("span_id", "spanId", "observationId")):
        return True
    return any(key in record for key in _JSON_SPAN_ROW_ONLY_KEYS)


def _validate_json_trace_record(record: dict[str, Any]) -> None:
    try:
        _JsonTraceRecordContract.model_validate(record)
    except ValidationError as exc:
        ctx = exc.errors()[0]["ctx"]
        raise ValueError(str(ctx["error"])) from exc


def _validate_json_span_row(record: dict[str, Any]) -> None:
    try:
        _JsonSpanRowContract.model_validate(record)
    except ValidationError as exc:
        ctx = exc.errors()[0]["ctx"]
        raise ValueError(str(ctx["error"])) from exc


def _group_json_span_rows(
    records: list[dict[str, Any]],
    limit: int,
    trace_source: TraceSource,
) -> list[TraceView]:
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for record in records:
        _validate_json_span_row(record)
        trace_id = _required_span_trace_id(record)
        grouped.setdefault(trace_id, []).append(record)
    traces: list[TraceView] = []
    for trace_id, rows in grouped.items():
        spans = [_json_span_view(row, trace_id=trace_id) for row in rows]
        traces.append(_trace_view_from_grouped_spans(trace_id, rows, spans, trace_source))
        if len(traces) >= limit:
            break
    return traces


def _json_record_trace_view(record: dict[str, Any], *, trace_source: TraceSource) -> TraceView:
    _validate_json_trace_record(record)
    trace_id = _required_trace_id(record)
    spans = [_json_span_view(span, trace_id=trace_id) for span in _dict_items(record.get("spans"))]
    explicit_status = _status_from_value(
        _first_value(record, "status", "status_code", "statusCode")
    )
    started_at = _started_at_unix_nano(record)
    ended_at = _ended_at_unix_nano(record)
    if spans:
        started_at = started_at if started_at is not None else _min_span_start(spans)
        ended_at = ended_at if ended_at is not None else _max_span_end(spans)
    return TraceView(
        id=trace_id,
        name=_string_or_none(_first_value(record, "name", "traceName", "type")),
        source=trace_source.with_trace_url(
            _string_or_none(_first_value(record, "trace_url", "traceUrl", "url"))
        ),
        started_at_unix_nano=started_at,
        ended_at_unix_nano=ended_at,
        duration_ms=_duration_ms(started_at, ended_at),
        status=_aggregate_status(explicit_status, spans),
        input=_first_value(record, "input", "inputs"),
        output=_first_value(record, "output", "outputs"),
        attributes=_attributes_from_mapping(record, _JSON_TRACE_RESERVED_KEYS),
        spans=spans,
        raw=record,
    )


def _trace_view_from_grouped_spans(
    trace_id: str,
    rows: list[dict[str, Any]],
    spans: list[SpanView],
    trace_source: TraceSource,
) -> TraceView:
    started_at = _min_span_start(spans)
    ended_at = _max_span_end(spans)
    return TraceView(
        id=trace_id,
        name=spans[0].name if spans else None,
        source=trace_source,
        started_at_unix_nano=started_at,
        ended_at_unix_nano=ended_at,
        duration_ms=_duration_ms(started_at, ended_at),
        status=_aggregate_status("unknown", spans),
        input=None,
        output=None,
        attributes={},
        spans=spans,
        raw=rows,
    )


def _json_span_view(span: dict[str, Any], *, trace_id: str) -> SpanView:
    span_id = _required_span_id(span, trace_id=trace_id)
    attrs = _attributes_from_mapping(span, _JSON_SPAN_RESERVED_KEYS)
    tool_name = _string_or_none(
        _first_value(span, "tool_name", "toolName")
        or _first_value(attrs, "kensa.tool.name", "tool.name", "openinference.tool.name")
    )
    raw_kind = _first_value(span, "kind", "type") or attrs.get("kensa.span.kind")
    kind = _normalized_kind(raw_kind, tool_name=tool_name)
    started_at = _started_at_unix_nano(span)
    ended_at = _ended_at_unix_nano(span)
    return SpanView(
        id=span_id,
        trace_id=_string_or_none(_first_value(span, "trace_id", "traceId")) or trace_id,
        parent_id=_string_or_none(
            _first_value(span, "parent_span_id", "parentSpanId", "parent_id")
        ),
        name=_string_or_none(_first_value(span, "name")) or kind,
        kind=kind,
        tool_name=tool_name,
        started_at_unix_nano=started_at,
        ended_at_unix_nano=ended_at,
        duration_ms=_duration_ms(started_at, ended_at),
        status=_status_from_value(_first_value(span, "status", "status_code", "statusCode")),
        status_message=_string_or_none(_first_value(span, "status_message", "status.message")),
        input=_first_value(span, "input", "inputs"),
        output=_first_value(span, "output", "outputs"),
        attributes=attrs,
        events=_dict_items(span.get("events", [])),
        raw=span,
    )


def _import_otlp_trace_views(
    data: Any,
    limit: int,
    trace_source: TraceSource,
) -> list[TraceView]:
    resource_spans = data.get("resourceSpans", []) if isinstance(data, dict) else []
    grouped: OrderedDict[str, list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]] = (
        OrderedDict()
    )
    for resource_span in _dict_items(resource_spans):
        resource = _dict_or_empty(resource_span.get("resource"))
        resource_attrs = _attributes(resource.get("attributes", []))
        for scope_span in _dict_items(resource_span.get("scopeSpans", [])):
            scope = _dict_or_empty(scope_span.get("scope"))
            scope_payload = {
                "name": scope.get("name"),
                "version": scope.get("version"),
                "attributes": _attributes(scope.get("attributes", [])),
            }
            for span in _dict_items(scope_span.get("spans", [])):
                trace_id = _required_key(span, "traceId", label="OTLP span trace id")
                grouped.setdefault(trace_id, []).append((span, resource_attrs, scope_payload))
    traces: list[TraceView] = []
    for trace_id, span_rows in grouped.items():
        spans = [
            _otlp_span_view(span, trace_id, resource_attrs, scope)
            for span, resource_attrs, scope in span_rows
        ]
        started_at = _min_span_start(spans)
        ended_at = _max_span_end(spans)
        first_resource = span_rows[0][1] if span_rows else {}
        first_scope = span_rows[0][2] if span_rows else {}
        attrs: dict[str, Any] = {}
        if first_resource:
            attrs["resource_attributes"] = first_resource
        if first_scope:
            attrs["instrumentation_scope"] = first_scope
        traces.append(
            TraceView(
                id=trace_id,
                name=spans[0].name if spans else None,
                source=trace_source,
                started_at_unix_nano=started_at,
                ended_at_unix_nano=ended_at,
                duration_ms=_duration_ms(started_at, ended_at),
                status=_aggregate_status("unknown", spans),
                input=None,
                output=None,
                attributes=attrs,
                spans=spans,
                raw=[span for span, _resource_attrs, _scope in span_rows],
            )
        )
        if len(traces) >= limit:
            break
    return traces


def _otlp_span_view(
    span: dict[str, Any],
    trace_id: str,
    resource_attrs: dict[str, Any],
    scope: dict[str, Any],
) -> SpanView:
    attrs = _attributes(span.get("attributes", []))
    if resource_attrs:
        attrs["resource_attributes"] = resource_attrs
    if scope:
        attrs["instrumentation_scope"] = scope
    links = _otlp_links(span.get("links", []))
    if links:
        attrs["links"] = links
    status = _dict_or_empty(span.get("status"))
    started_at = _unix_nano_or_none(span.get("startTimeUnixNano"))
    ended_at = _unix_nano_or_none(span.get("endTimeUnixNano"))
    kind = _normalized_kind(span.get("kind"), tool_name=_tool_name_from_attrs(attrs))
    return SpanView(
        id=_required_key(span, "spanId", label=f"span id for trace {trace_id}"),
        trace_id=trace_id,
        parent_id=_string_or_none(span.get("parentSpanId") or None),
        name=_string_or_none(span.get("name")) or kind,
        kind=kind,
        tool_name=_tool_name_from_attrs(attrs),
        started_at_unix_nano=started_at,
        ended_at_unix_nano=ended_at,
        duration_ms=_duration_ms(started_at, ended_at),
        status=_status_from_value(status.get("code")),
        status_message=_string_or_none(status.get("message")),
        input=None,
        output=None,
        attributes=attrs,
        events=_otlp_events(span.get("events", [])),
        raw=span,
    )


def _import_langfuse_trace_views(
    data: Any,
    limit: int,
    trace_source: TraceSource,
) -> list[TraceView]:
    if isinstance(data, dict) and "data" in data:
        return _langfuse_observation_trace_views(data.get("data", []), limit, trace_source)
    raw_traces = data.get("traces", data) if isinstance(data, dict) else data
    traces = _dict_items(raw_traces) if isinstance(raw_traces, list) else _dict_items([raw_traces])
    top_level_observations = _observations_by_trace(
        data.get("observations", []) if isinstance(data, dict) else []
    )
    views: list[TraceView] = []
    for trace in traces:
        trace_id = _required_trace_id(trace)
        observations = [
            *_dict_items(trace.get("observations", [])),
            *top_level_observations.get(trace_id, []),
        ]
        spans = [
            _langfuse_observation_span_view(observation, trace_id) for observation in observations
        ]
        started_at = _started_at_unix_nano(trace)
        ended_at = _ended_at_unix_nano(trace)
        if spans:
            started_at = started_at if started_at is not None else _min_span_start(spans)
            ended_at = ended_at if ended_at is not None else _max_span_end(spans)
        views.append(
            TraceView(
                id=trace_id,
                name=_string_or_none(_first_value(trace, "name", "traceName", "type")),
                source=trace_source.with_trace_url(
                    _string_or_none(_first_value(trace, "trace_url", "traceUrl", "url"))
                ),
                started_at_unix_nano=started_at,
                ended_at_unix_nano=ended_at,
                duration_ms=_duration_ms(started_at, ended_at),
                status=_aggregate_status(
                    _status_from_value(_first_value(trace, "status", "level", "error")),
                    spans,
                ),
                input=_first_value(trace, "input"),
                output=_first_value(trace, "output"),
                attributes=_langfuse_attributes(trace),
                spans=spans,
                raw=trace,
            )
        )
        if len(views) >= limit:
            break
    return views


def _langfuse_observation_trace_views(
    observations_value: Any,
    limit: int,
    trace_source: TraceSource,
) -> list[TraceView]:
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for observation in _dict_items(observations_value):
        trace_id = _required_span_trace_id(observation)
        grouped.setdefault(trace_id, []).append(observation)
    views: list[TraceView] = []
    for trace_id, observations in grouped.items():
        spans = [
            _langfuse_observation_span_view(observation, trace_id) for observation in observations
        ]
        started_at = _min_span_start(spans)
        ended_at = _max_span_end(spans)
        first = observations[0] if observations else {}
        views.append(
            TraceView(
                id=trace_id,
                name=_string_or_none(_first_value(first, "traceName", "trace_name")) or trace_id,
                source=trace_source.with_trace_url(
                    _string_or_none(_first_value(first, "trace_url", "traceUrl", "url"))
                ),
                started_at_unix_nano=started_at,
                ended_at_unix_nano=ended_at,
                duration_ms=_duration_ms(started_at, ended_at),
                status=_aggregate_status("unknown", spans),
                input=None,
                output=None,
                attributes=_langfuse_observation_trace_attributes(first),
                spans=spans,
                raw=observations,
            )
        )
        if len(views) >= limit:
            break
    return views


def _langfuse_observation_span_view(
    observation: dict[str, Any],
    trace_id: str,
) -> SpanView:
    observation_type = str(
        _first_value(observation, "type", "observationType", "observation_type") or "span"
    )
    name = str(_first_value(observation, "name", "id") or observation_type)
    span_id = _required_span_id(observation, trace_id=trace_id)
    parent_id = _first_value(observation, "parentObservationId", "parent_observation_id")
    tool_name = (
        name
        if observation_type.lower() == "tool"
        else _tool_name_from_attrs(_dict_or_empty(observation.get("metadata")))
    )
    started_at = _started_at_unix_nano(observation)
    ended_at = _ended_at_unix_nano(observation)
    return SpanView(
        id=span_id,
        trace_id=_string_or_none(_first_value(observation, "traceId", "trace_id")) or trace_id,
        parent_id=_string_or_none(parent_id),
        name=name,
        kind=_normalized_kind(observation_type, tool_name=tool_name),
        tool_name=tool_name,
        started_at_unix_nano=started_at,
        ended_at_unix_nano=ended_at,
        duration_ms=_duration_ms(started_at, ended_at),
        status=_status_from_value(_first_value(observation, "status", "level", "error")),
        status_message=_string_or_none(
            _first_value(observation, "status_message", "statusMessage")
        ),
        input=_first_value(observation, "input", "inputs"),
        output=_first_value(observation, "output", "outputs"),
        attributes=_langfuse_attributes(observation),
        events=_dict_items(observation.get("events", [])),
        raw=observation,
    )


_LANGFUSE_RESERVED_KEYS = frozenset(
    {
        "endTime",
        "end_time",
        "events",
        "id",
        "input",
        "inputs",
        "metadata",
        "observations",
        "observationId",
        "observation_type",
        "output",
        "outputs",
        "parentObservationId",
        "parent_observation_id",
        "sessionId",
        "startTime",
        "start_time",
        "status",
        "statusMessage",
        "status_message",
        "timestamp",
        "traceId",
        "traceName",
        "traceSessionId",
        "traceUserId",
        "trace_id",
        "trace_name",
        "trace_session_id",
        "trace_user_id",
        "trace_url",
        "traceUrl",
        "url",
        "userId",
    }
)


def _langfuse_attributes(row: dict[str, Any]) -> dict[str, Any]:
    attrs = _attributes_from_mapping(row, _LANGFUSE_RESERVED_KEYS)
    for key in (
        "user_id",
        "userId",
        "traceUserId",
        "trace_user_id",
        "session_id",
        "sessionId",
        "traceSessionId",
        "trace_session_id",
        "traceName",
        "trace_name",
        "release",
        "version",
        "environment",
        "feedback",
        "feedback_stats",
        "scores",
    ):
        if key in row and row[key] is not None:
            attrs[key] = row[key]
    return attrs


def _langfuse_observation_trace_attributes(row: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for key in (
        "traceName",
        "trace_name",
        "traceUserId",
        "trace_user_id",
        "traceSessionId",
        "trace_session_id",
        "userId",
        "user_id",
        "sessionId",
        "session_id",
        "release",
        "version",
        "environment",
        "tags",
    ):
        if key in row and row[key] is not None:
            attrs[key] = row[key]
    metadata = row.get("metadata")
    if isinstance(metadata, dict):
        attrs.update({str(key): value for key, value in metadata.items()})
    return attrs


def _observations_by_trace(value: Any) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for observation in _dict_items(value):
        trace_id = _first_value(observation, "traceId", "trace_id")
        if trace_id is not None:
            grouped.setdefault(str(trace_id), []).append(observation)
    return grouped


def _first_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _required_trace_id(record: dict[str, Any]) -> str:
    value = _first_value(record, "id", "trace_id", "traceId")
    if value is None or str(value) == "":
        raise ValueError("trace record is missing a mechanically derivable trace id")
    return str(value)


def _required_span_trace_id(record: dict[str, Any]) -> str:
    value = _first_value(record, "trace_id", "traceId")
    if value is None or str(value) == "":
        raise ValueError("span record is missing a mechanically derivable trace id")
    return str(value)


def _required_span_id(span: dict[str, Any], *, trace_id: str) -> str:
    value = _first_value(span, "span_id", "spanId", "id", "observationId")
    if value is None or str(value) == "":
        raise ValueError(f"span in trace {trace_id} is missing a mechanically derivable span id")
    return str(value)


def _required_key(row: dict[str, Any], key: str, *, label: str) -> str:
    value = row.get(key)
    if value is None or str(value) == "":
        raise ValueError(f"{label} is missing")
    return str(value)


def _attributes_from_mapping(
    row: dict[str, Any],
    reserved_keys: frozenset[str],
) -> dict[str, Any]:
    attrs = _dict_or_empty(row.get("attributes"))
    metadata = row.get("metadata")
    if isinstance(metadata, dict):
        attrs.update({str(key): value for key, value in metadata.items()})
    for key, value in row.items():
        if key in reserved_keys or key in {"attributes", "metadata"}:
            continue
        attrs[str(key)] = value
    return attrs


def _started_at_unix_nano(row: dict[str, Any]) -> int | None:
    return _unix_nano_or_none(
        _first_value(
            row,
            "started_at_unix_nano",
            "start_time_unix_nano",
            "startTimeUnixNano",
            "startTime",
            "start_time",
            "timestamp",
        )
    )


def _ended_at_unix_nano(row: dict[str, Any]) -> int | None:
    return _unix_nano_or_none(
        _first_value(
            row,
            "ended_at_unix_nano",
            "end_time_unix_nano",
            "endTimeUnixNano",
            "endTime",
            "end_time",
        )
    )


def _unix_nano_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1_000_000_000)
    return None


def _duration_ms(started_at: int | None, ended_at: int | None) -> float:
    if started_at is None or ended_at is None:
        return 0.0
    return max(0.0, (ended_at - started_at) / 1_000_000)


def _min_span_start(spans: list[SpanView]) -> int | None:
    starts = [span.started_at_unix_nano for span in spans if span.started_at_unix_nano is not None]
    return min(starts) if starts else None


def _max_span_end(spans: list[SpanView]) -> int | None:
    ends = [span.ended_at_unix_nano for span in spans if span.ended_at_unix_nano is not None]
    return max(ends) if ends else None


def _aggregate_status(
    explicit_status: Literal["ok", "error", "unknown"],
    spans: list[SpanView],
) -> Literal["ok", "error", "unknown"]:
    if explicit_status != "unknown":
        return explicit_status
    statuses = [span.status for span in spans]
    if any(status == "error" for status in statuses):
        return "error"
    if statuses and all(status == "ok" for status in statuses):
        return "ok"
    return "unknown"


def _string_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _normalized_kind(value: Any, *, tool_name: str | None) -> str:
    if value is None:
        return "tool" if tool_name else "span"
    text = str(value).strip().lower()
    if text == "tool" or tool_name:
        return "tool"
    if text in {"generation", "llm"}:
        return "llm"
    return text or "span"


def _tool_name_from_attrs(attrs: dict[str, Any]) -> str | None:
    return _string_or_none(
        _first_value(attrs, "tool.name", "kensa.tool.name", "openinference.tool.name")
    )


def _status_from_value(value: Any) -> Literal["ok", "error", "unknown"]:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "error" if value else "ok"
    text = str(value).strip().lower()
    if not text:
        return "unknown"
    if "error" in text or "fail" in text or text in {"2", "false", "ko"}:
        return "error"
    if text in {
        "0",
        "1",
        "200",
        "default",
        "ok",
        "pass",
        "passed",
        "success",
        "succeeded",
        "status_code_ok",
        "status_code_unset",
        "true",
        "unset",
    }:
        return "ok"
    return "unknown"


def _attributes(value: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for item in _dict_items(value):
        key = item.get("key")
        if key is not None:
            attrs[str(key)] = _otlp_value(item.get("value"))
    return attrs


def _otlp_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "intValue", "doubleValue", "boolValue", "bytesValue"):
        if key in value:
            return value[key]
    if "arrayValue" in value:
        raw_values = value["arrayValue"].get("values", [])
        return [_otlp_value(item) for item in raw_values]
    if "kvlistValue" in value:
        return _attributes(value["kvlistValue"].get("values", []))
    return value


def _otlp_events(value: Any) -> list[dict[str, Any]]:
    return [
        {
            "name": event.get("name"),
            "time_unix_nano": _int_or_none(event.get("timeUnixNano")),
            "attributes": _attributes(event.get("attributes", [])),
        }
        for event in _dict_items(value)
    ]


def _otlp_links(value: Any) -> list[dict[str, Any]]:
    return [
        {
            "trace_id": link.get("traceId"),
            "span_id": link.get("spanId"),
            "trace_state": link.get("traceState"),
            "attributes": _attributes(link.get("attributes", [])),
        }
        for link in _dict_items(value)
    ]


def import_redaction_manifest(artifact: Path | str) -> Any:
    """Read the redaction block from an artifact's sibling manifest.

    The sibling `<artifact>.manifest.json` file is the source of truth for
    manifest lookup; `latest.json` pointers are a convenience only. Returns
    None when the sibling manifest is absent or unreadable, which exposure
    gates treat as unsafe.
    """

    manifest_path = Path(artifact).with_suffix(".manifest.json")
    try:
        payload = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload.get("redaction")


def load_trace_views(source: Path | str) -> list[dict[str, Any]]:
    """Load TraceView rows behind the mandatory payload-exposure gate.

    This is the single choke point for trace payload exposure: `traces list`,
    `traces sample`, `traces get`, and `inspect` load rows through it. Unsafe or
    missing redaction manifests block every caller, including generation workflows.
    """

    path = Path(source)
    assert_safe_manifest(import_redaction_manifest(path))
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise ValueError(f"Could not read trace import artifact {path}: {exc}") from exc
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(_reimport_trace_message(path)) from exc
        if not isinstance(row, dict):
            raise ValueError(_reimport_trace_message(path))
        _validate_trace_view_row(row, path=path, line_number=index)
        rows.append(row)
    return rows


def trace_view_summary(trace: dict[str, Any]) -> dict[str, Any]:
    source = trace["source"]
    return {
        "id": trace["id"],
        "name": trace["name"],
        "status": trace["status"],
        "started_at_unix_nano": trace["started_at_unix_nano"],
        "duration_ms": trace["duration_ms"],
        "span_count": len(trace["spans"]),
        "source": {
            "provider": source["provider"],
            "trace_url": source["trace_url"],
        },
    }


def _validate_trace_view_row(row: dict[str, Any], *, path: Path, line_number: int) -> None:
    if row.get("schema_version") != TRACE_VIEW_SCHEMA_VERSION:
        raise ValueError(_reimport_trace_message(path))
    if tuple(row.keys()) != _TRACE_VIEW_KEYS and set(row) != set(_TRACE_VIEW_KEYS):
        raise ValueError(_reimport_trace_message(path))
    source = row.get("source")
    if not isinstance(source, dict) or set(source) != set(_TRACE_SOURCE_KEYS):
        raise ValueError(_reimport_trace_message(path))
    spans = row.get("spans")
    if not isinstance(spans, list):
        raise ValueError(_reimport_trace_message(path))
    for span in spans:
        if not isinstance(span, dict) or set(span) != set(_SPAN_VIEW_KEYS):
            raise ValueError(_reimport_trace_message(path))
    if not isinstance(row.get("id"), str) or not row["id"]:
        raise ValueError(f"TraceView row {line_number} in {path} is missing id")


def _reimport_trace_message(path: Path) -> str:
    return (
        f"Unsupported trace import artifact format at {path}. Re-import traces with kensa import."
    )


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dict_or_empty(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "TRACE_MANIFEST_SCHEMA_VERSION",
    "TRACE_VIEW_SCHEMA_VERSION",
    "ImportResult",
    "SpanView",
    "TraceManifest",
    "TraceSource",
    "TraceView",
    "import_redaction_manifest",
    "import_trace_records",
    "import_trace_source",
    "load_trace_views",
    "safe_endpoint",
    "trace_timestamp",
    "trace_view_summary",
    "write_trace_manifest",
]
