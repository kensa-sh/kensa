from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest
from conftest import FakeRedactionEnv
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from kensa import redact
from kensa import traces as traces_module
from kensa.traces import (
    OtlpTraceLimits,
    OtlpTraceProcessingError,
    OtlpTraceProcessor,
    TraceView,
)


def _request() -> ExportTraceServiceRequest:
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    scope_spans = resource_spans.scope_spans.add()
    span = scope_spans.spans.add()
    span.trace_id = bytes.fromhex("10" * 16)
    span.span_id = bytes.fromhex("20" * 8)
    span.name = "agent.run"
    span.start_time_unix_nano = 1_000_000_000
    span.end_time_unix_nano = 2_000_000_000

    prompt = span.attributes.add()
    prompt.key = "gen_ai.prompt"
    prompt.value.string_value = "Alice uses tok_live"
    input_tokens = span.attributes.add()
    input_tokens.key = "gen_ai.usage.input_tokens"
    input_tokens.value.int_value = 42
    cost = span.attributes.add()
    cost.key = "kensa.cost_usd"
    cost.value.double_value = 0.25
    event = span.events.add()
    event.attributes.add(key="event.detail").value.string_value = "ignored"
    link = span.links.add()
    link.trace_id = bytes.fromhex("30" * 16)
    link.span_id = bytes.fromhex("40" * 8)
    link.attributes.add(key="link.detail").value.string_value = "ignored"
    return request


def _payload() -> bytes:
    return _request().SerializeToString()


def _request_without_spans() -> bytes:
    request = ExportTraceServiceRequest()
    request.resource_spans.add()
    return request.SerializeToString()


def test_processes_redacted_trace_views_without_writing_artifacts(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    processor = OtlpTraceProcessor(
        pseudonym_key=b"k" * 32,
        omit_usage_metrics=True,
    )

    first = processor.process(_payload())
    second = processor.process(_payload())

    assert len(first) == 1
    trace = first[0]
    assert isinstance(trace, TraceView)
    assert trace.id.startswith("trace_")
    assert trace.spans[0].id.startswith("span_")
    assert second[0].id == trace.id
    assert second[0].spans == trace.spans
    assert trace.spans[0].trace_id == trace.id
    assert trace.spans[0].input == "[PERSON_1] uses [SECRET_1]"
    assert trace.spans[0].usage.input_tokens is None
    assert trace.spans[0].usage.cost_usd is None
    assert not (tmp_path / ".kensa").exists()


def test_keeps_usage_metrics_by_default(redaction_ready: FakeRedactionEnv) -> None:
    trace = OtlpTraceProcessor(pseudonym_key=b"k" * 32).process(_payload())[0]

    assert trace.spans[0].usage.input_tokens == 42
    assert trace.spans[0].usage.cost_usd == 0.25


@pytest.mark.parametrize(
    "key",
    [b"", b"x" * 31, b"x" * 33, cast(Any, bytearray(b"x" * 32))],
)
def test_rejects_invalid_pseudonym_keys(key: bytes) -> None:
    with pytest.raises(OtlpTraceProcessingError, match="exactly 32 bytes"):
        OtlpTraceProcessor(pseudonym_key=key)


@pytest.mark.parametrize("value", [0, -1, True, cast(Any, "1")])
def test_rejects_invalid_limits(value: int) -> None:
    limits = replace(OtlpTraceLimits(), max_spans=value)

    with pytest.raises(OtlpTraceProcessingError, match="max_spans"):
        OtlpTraceProcessor(pseudonym_key=b"k" * 32, limits=limits)


def test_rejects_invalid_attribute_depth_limit() -> None:
    limits = replace(OtlpTraceLimits(), max_attribute_depth=0)

    with pytest.raises(OtlpTraceProcessingError, match="max_attribute_depth"):
        OtlpTraceProcessor(pseudonym_key=b"k" * 32, limits=limits)


@pytest.mark.parametrize(
    ("payload", "limits", "message"),
    [
        (cast(Any, "protobuf"), OtlpTraceLimits(), "must be bytes"),
        (b"", OtlpTraceLimits(), "is empty"),
        (b"\x80", OtlpTraceLimits(), "is malformed"),
        (_payload(), OtlpTraceLimits(max_payload_bytes=1), "exceeds 1 bytes"),
        (_request_without_spans(), OtlpTraceLimits(), "no traces"),
    ],
)
def test_rejects_invalid_payloads(
    payload: bytes,
    limits: OtlpTraceLimits,
    message: str,
    redaction_ready: FakeRedactionEnv,
) -> None:
    processor = OtlpTraceProcessor(pseudonym_key=b"k" * 32, limits=limits)

    with pytest.raises(OtlpTraceProcessingError, match=message):
        processor.process(payload)


@pytest.mark.parametrize(
    ("target", "value"),
    [
        ("trace_id", b"short"),
        ("trace_id", b"\0" * 16),
        ("span_id", b"short"),
        ("span_id", b"\0" * 8),
        ("parent_span_id", b"short"),
        ("parent_span_id", b"\0" * 8),
        ("link_trace_id", b"short"),
        ("link_trace_id", b"\0" * 16),
        ("link_span_id", b"short"),
        ("link_span_id", b"\0" * 8),
    ],
)
def test_rejects_invalid_identifiers(
    target: str,
    value: bytes,
    redaction_ready: FakeRedactionEnv,
) -> None:
    request = _request()
    span = request.resource_spans[0].scope_spans[0].spans[0]
    if target.startswith("link_"):
        link = span.links.add()
        link.trace_id = bytes.fromhex("30" * 16)
        link.span_id = bytes.fromhex("40" * 8)
        setattr(link, target.removeprefix("link_"), value)
    else:
        setattr(span, target, value)

    with pytest.raises(OtlpTraceProcessingError, match="invalid"):
        OtlpTraceProcessor(pseudonym_key=b"k" * 32).process(request.SerializeToString())


@pytest.mark.parametrize(
    "limit_name",
    [
        "max_traces",
        "max_resource_spans",
        "max_scope_spans",
        "max_spans",
        "max_attributes",
        "max_events",
        "max_links",
    ],
)
def test_enforces_nested_limits_before_json_expansion(
    limit_name: str,
    redaction_ready: FakeRedactionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    first_resource = request.resource_spans[0]
    first_scope = first_resource.scope_spans[0]
    first_span = first_scope.spans[0]
    if limit_name == "max_traces":
        second = first_scope.spans.add()
        second.CopyFrom(first_span)
        second.trace_id = bytes.fromhex("11" * 16)
        second.span_id = bytes.fromhex("21" * 8)
    elif limit_name == "max_resource_spans":
        request.resource_spans.add()
    elif limit_name == "max_scope_spans":
        first_resource.scope_spans.add()
    elif limit_name == "max_spans":
        second = first_scope.spans.add()
        second.CopyFrom(first_span)
        second.span_id = bytes.fromhex("21" * 8)
    elif limit_name == "max_attributes":
        first_resource.resource.attributes.add(key="extra")
    elif limit_name == "max_events":
        first_span.events.add()
        first_span.events.add()
    else:
        first_span.links.add()
        first_span.links.add()

    def fail_json_expansion(*args: object, **kwargs: object) -> Any:
        raise AssertionError("protobuf-to-JSON expansion must not run")

    monkeypatch.setattr(traces_module, "_otlp_message_to_dict", fail_json_expansion)
    limits = replace(OtlpTraceLimits(), **{limit_name: 1})
    processor = OtlpTraceProcessor(pseudonym_key=b"k" * 32, limits=limits)

    with pytest.raises(OtlpTraceProcessingError, match="too many"):
        processor.process(request.SerializeToString())


@pytest.mark.parametrize("nested_kind", ["array", "key_value_list"])
def test_counts_nested_attribute_values_before_json_expansion(
    nested_kind: str,
    redaction_ready: FakeRedactionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    span = request.resource_spans[0].scope_spans[0].spans[0]
    span.ClearField("attributes")
    span.ClearField("events")
    span.ClearField("links")
    attribute = span.attributes.add(key="nested")
    if nested_kind == "array":
        attribute.value.array_value.values.add().string_value = "one"
        attribute.value.array_value.values.add().string_value = "two"
    else:
        attribute.value.kvlist_value.values.add(key="one").value.string_value = "one"
        attribute.value.kvlist_value.values.add(key="two").value.string_value = "two"

    def fail_json_expansion(*args: object, **kwargs: object) -> Any:
        raise AssertionError("protobuf-to-JSON expansion must not run")

    monkeypatch.setattr(traces_module, "_otlp_message_to_dict", fail_json_expansion)
    processor = OtlpTraceProcessor(
        pseudonym_key=b"k" * 32,
        limits=OtlpTraceLimits(max_attributes=2),
    )

    with pytest.raises(OtlpTraceProcessingError, match="too many attribute values"):
        processor.process(request.SerializeToString())


def test_rejects_deeply_nested_attribute_values_before_json_expansion(
    redaction_ready: FakeRedactionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    span = request.resource_spans[0].scope_spans[0].spans[0]
    span.ClearField("attributes")
    span.ClearField("events")
    span.ClearField("links")
    value = span.attributes.add(key="nested").value
    value = value.array_value.values.add()
    value = value.array_value.values.add()
    value.string_value = "too deep"

    def fail_json_expansion(*args: object, **kwargs: object) -> Any:
        raise AssertionError("protobuf-to-JSON expansion must not run")

    monkeypatch.setattr(traces_module, "_otlp_message_to_dict", fail_json_expansion)
    processor = OtlpTraceProcessor(
        pseudonym_key=b"k" * 32,
        limits=OtlpTraceLimits(max_attribute_depth=2),
    )

    with pytest.raises(OtlpTraceProcessingError, match="too deeply nested"):
        processor.process(request.SerializeToString())


def test_wraps_redaction_failures_atomically(redaction_ready: FakeRedactionEnv) -> None:
    redaction_ready.analyzer_error = RuntimeError("model exploded")
    processor = OtlpTraceProcessor(pseudonym_key=b"k" * 32)

    with pytest.raises(OtlpTraceProcessingError, match="could not be processed safely"):
        processor.process(_payload())


def test_wraps_redactor_initialization_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_redactor(*args: object, **kwargs: object) -> Any:
        raise ValueError("not ready")

    monkeypatch.setattr(traces_module, "Redactor", fail_redactor)

    with pytest.raises(OtlpTraceProcessingError, match="redaction is unavailable"):
        OtlpTraceProcessor(pseudonym_key=b"k" * 32)


def test_reuses_one_engine_and_serializes_mutable_redaction_state(
    redaction_ready: FakeRedactionEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_calls = 0
    original_load_engine = redact._load_engine

    def counting_load_engine(readiness: redact.RedactionReadiness) -> Any:
        nonlocal load_calls
        load_calls += 1
        return original_load_engine(readiness)

    monkeypatch.setattr(redact, "_load_engine", counting_load_engine)
    processor = OtlpTraceProcessor(pseudonym_key=b"k" * 32)
    first_entered = Event()
    release_first = Event()
    second_entered = Event()
    original_redact_trace_view = processor._redactor.redact_trace_view
    calls = 0

    def blocking_redact_trace_view(trace: dict[str, Any]) -> redact.RedactionResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_entered.set()
            assert release_first.wait(timeout=5)
        else:
            second_entered.set()
        return original_redact_trace_view(trace)

    monkeypatch.setattr(processor._redactor, "redact_trace_view", blocking_redact_trace_view)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(processor.process, _payload())
        assert first_entered.wait(timeout=5)
        second = executor.submit(processor.process, _payload())
        assert not second_entered.wait(timeout=0.1)
        release_first.set()
        results = (first.result(timeout=5), second.result(timeout=5))

    assert load_calls == 1
    assert second_entered.is_set()
    assert all(result[0].spans[0].input == "[PERSON_1] uses [SECRET_1]" for result in results)
