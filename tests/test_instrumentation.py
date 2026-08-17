from __future__ import annotations

import gc
import hashlib
import json
import math
import os
import socket
import subprocess
import sys
import threading
import weakref
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from opentelemetry.trace import SpanKind

import kensa
from kensa import cli_traces, redact, tracing
from kensa.tracing import record_llm_call, record_span, record_tool_call


def test_instrument_noops_without_trace_dir(monkeypatch) -> None:
    monkeypatch.delenv("KENSA_TRACE_DIR", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.setattr(
        tracing,
        "_add_span_processors_once",
        lambda *args, **kwargs: pytest.fail("instrument should no-op"),
    )

    kensa.instrument()


def test_instrument_dual_exports_to_otlp_http_and_jsonl(tmp_path: Path) -> None:
    requests: list[tuple[str, bytes, str | None, str | None]] = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            requests.append(
                (
                    self.path,
                    self.rfile.read(length),
                    self.headers.get("Content-Type"),
                    self.headers.get("x-test"),
                )
            )
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    server = HTTPServer(("127.0.0.1", 0), Collector)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/traces"
        script = f"""from pathlib import Path
from opentelemetry import trace
from kensa import instrument, record_span

instrument(
    Path({str(tmp_path)!r}),
    otlp_endpoint={endpoint!r},
    otlp_headers={{"x-test": "dual-export"}},
)
with record_span("dual-export"):
    pass
assert trace.get_tracer_provider().force_flush(1_000)
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert completed.returncode == 0, completed.stderr
    rows = [json.loads(line) for line in (tmp_path / "spans.jsonl").read_text().splitlines()]
    assert rows[-1]["name"] == "dual-export"
    assert requests
    assert requests[-1][0] == "/v1/traces"
    assert requests[-1][1]
    assert requests[-1][2] == "application/x-protobuf"
    assert requests[-1][3] == "dual-export"


def test_unreachable_otlp_endpoint_reports_failure_without_losing_local_span(
    tmp_path: Path,
) -> None:
    closed_socket = socket.socket()
    closed_socket.bind(("127.0.0.1", 0))
    port = int(closed_socket.getsockname()[1])
    closed_socket.close()
    endpoint = f"http://127.0.0.1:{port}/v1/traces"
    script = f"""from pathlib import Path
from kensa import instrument, record_span

instrument(Path({str(tmp_path)!r}), otlp_endpoint={endpoint!r})
with record_span("local-survives"):
    pass
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )

    assert completed.returncode == 0
    rows = [json.loads(line) for line in (tmp_path / "spans.jsonl").read_text().splitlines()]
    assert rows[-1]["name"] == "local-survives"
    assert "OTLP HTTP span export failed" in completed.stderr


def test_otlp_endpoint_environment_enables_export_and_default_local_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exported: list[str] = []

    class Exporter:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["endpoint"] == "http://kensa-collector.example/v1/traces"
            assert kwargs["timeout"] == tracing._DEFAULT_OTLP_TIMEOUT_S

        def export(self, spans: Any) -> Any:
            exported.extend(span.name for span in spans)
            return tracing.SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            del timeout_millis
            return True

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KENSA_OTLP_ENDPOINT", "http://kensa-collector.example/v1/traces")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.example")
    monkeypatch.setattr(tracing, "_otlp_span_exporter_class", lambda: Exporter)

    kensa.instrument()
    with record_span("environment-export"):
        pass
    assert cast(Any, tracing.trace.get_tracer_provider()).force_flush(1_000)

    assert exported[-1] == "environment-export"
    assert (tmp_path / ".kensa" / "traces" / "spans.jsonl").exists()


@pytest.mark.parametrize(
    ("endpoint_env", "explicit_endpoint", "expected_endpoint"),
    [
        (None, None, None),
        ("   ", None, None),
        ("http://from-env.example/v1/traces", None, "http://from-env.example/v1/traces"),
        (None, "http://explicit.example/v1/traces", "http://explicit.example/v1/traces"),
        (
            "http://from-env.example/v1/traces",
            "http://explicit.example/v1/traces",
            "http://explicit.example/v1/traces",
        ),
    ],
)
def test_otlp_export_requires_a_kensa_owned_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint_env: str | None,
    explicit_endpoint: str | None,
    expected_endpoint: str | None,
) -> None:
    class Provider:
        def __init__(self) -> None:
            self.processors: list[Any] = []

        def add_span_processor(self, processor: Any) -> None:
            self.processors.append(processor)

    provider = Provider()
    remote_processor = object()
    seen: list[Any] = []
    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setattr(
        tracing,
        "_build_otlp_http_processor",
        lambda **kwargs: (seen.append(kwargs["endpoint"]), cast(Any, remote_processor))[1],
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://injected.example")
    if endpoint_env is not None:
        monkeypatch.setenv("KENSA_OTLP_ENDPOINT", endpoint_env)

    kensa.instrument(tmp_path, otlp_endpoint=explicit_endpoint)

    assert len(provider.processors) == (2 if expected_endpoint else 1)
    assert seen == ([expected_endpoint] if expected_endpoint else [])


def test_standard_otel_environment_alone_does_not_instrument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        def __init__(self) -> None:
            self.processors: list[Any] = []

        def add_span_processor(self, processor: Any) -> None:
            self.processors.append(processor)

    provider = Provider()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.example")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "http://collector.example/v1/traces",
    )

    kensa.instrument()

    assert provider.processors == []
    assert not (tmp_path / ".kensa").exists()


@pytest.mark.parametrize(
    ("headers_env", "explicit_headers", "expected_headers"),
    [
        (None, None, None),
        ("   ", None, None),
        ("authorization=Bearer kensa-token", None, {"authorization": "Bearer kensa-token"}),
        ("authorization=Bearer kensa-token", {"x-api-key": "explicit"}, {"x-api-key": "explicit"}),
    ],
)
def test_otlp_headers_come_only_from_kensa_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    headers_env: str | None,
    explicit_headers: dict[str, str] | None,
    expected_headers: dict[str, str] | None,
) -> None:
    seen: list[Any] = []

    class Exporter:
        def __init__(self, **kwargs: Any) -> None:
            seen.append(
                {
                    "headers": kwargs["headers"],
                    "application_headers_visible": os.environ.get("OTEL_EXPORTER_OTLP_HEADERS"),
                }
            )

        def export(self, spans: Any) -> Any:
            del spans
            return tracing.SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            del timeout_millis
            return True

    class Provider:
        def __init__(self) -> None:
            self.processors: list[Any] = []

        def add_span_processor(self, processor: Any) -> None:
            self.processors.append(processor)

    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: Provider())
    monkeypatch.setattr(tracing, "_otlp_span_exporter_class", lambda: Exporter)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-honeycomb-team=application-secret")
    if headers_env is not None:
        monkeypatch.setenv("KENSA_OTLP_HEADERS", headers_env)

    kensa.instrument(
        tmp_path,
        otlp_endpoint="http://kensa-collector.example/v1/traces",
        otlp_headers=explicit_headers,
    )

    assert seen[0]["headers"] == expected_headers
    assert seen[0]["application_headers_visible"] is None
    assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == "x-honeycomb-team=application-secret"


def test_unavailable_local_capture_does_not_disable_otlp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Provider:
        def __init__(self) -> None:
            self.processors: list[Any] = []

        def add_span_processor(self, processor: Any) -> None:
            self.processors.append(processor)

    provider = Provider()
    remote_processor = object()
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory")
    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setattr(
        tracing,
        "_build_otlp_http_processor",
        lambda **kwargs: cast(Any, remote_processor),
    )

    with caplog.at_level("ERROR", logger="kensa.tracing"):
        kensa.instrument(
            blocked_parent / "traces",
            otlp_endpoint="http://127.0.0.1:4318/v1/traces",
        )

    assert provider.processors == [remote_processor]
    assert "local capture disabled" in caplog.text


def test_reporting_otlp_exporter_contains_export_flush_and_shutdown_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class WorkingExporter(tracing.SpanExporter):
        def export(self, spans: Any) -> Any:
            del spans
            return tracing.SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return timeout_millis == 123

    class FailingExporter(tracing.SpanExporter):
        def export(self, spans: Any) -> Any:
            del spans
            raise RuntimeError("export")

        def shutdown(self) -> None:
            raise RuntimeError("shutdown")

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            del timeout_millis
            raise RuntimeError("flush")

    class FailureResultExporter(WorkingExporter):
        def export(self, spans: Any) -> Any:
            del spans
            return tracing.SpanExportResult.FAILURE

    working = tracing._ReportingSpanExporter(WorkingExporter())
    working.shutdown()
    assert working.force_flush(123) is True

    failing = tracing._ReportingSpanExporter(FailingExporter())
    with caplog.at_level("ERROR", logger="kensa.tracing"):
        assert (
            tracing._ReportingSpanExporter(FailureResultExporter()).export([])
            is tracing.SpanExportResult.FAILURE
        )
        assert failing.export([]) is tracing.SpanExportResult.FAILURE
        failing.shutdown()
        assert failing.force_flush() is False
    assert "OTLP HTTP span export failed" in caplog.text
    assert "OTLP HTTP exporter shutdown failed" in caplog.text
    assert "OTLP HTTP exporter flush failed" in caplog.text


def test_instrument_registers_local_and_batched_otlp_processors_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        def __init__(self) -> None:
            self.processors: list[Any] = []

        def add_span_processor(self, processor: Any) -> None:
            self.processors.append(processor)

    class Exporter(tracing.SpanExporter):
        def export(self, spans: Any) -> Any:
            del spans
            return tracing.SpanExportResult.SUCCESS

    provider = Provider()
    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setattr(
        tracing,
        "_otlp_span_exporter_class",
        lambda: lambda **kwargs: Exporter(),
    )
    for _ in range(2):
        kensa.instrument(
            tmp_path,
            otlp_endpoint="http://127.0.0.1:4318/v1/traces",
            otlp_headers={"x-test": "idempotent"},
            otlp_timeout_s=1,
        )

    assert len(provider.processors) == 2
    assert isinstance(provider.processors[0], tracing.SimpleSpanProcessor)
    assert isinstance(provider.processors[1], tracing.BatchSpanProcessor)


@pytest.mark.parametrize(
    ("environment_name", "environment_value"),
    [
        ("OTEL_EXPORTER_OTLP_TIMEOUT", "10s"),
        ("OTEL_BSP_MAX_QUEUE_SIZE", "0"),
    ],
)
def test_invalid_otlp_configuration_preserves_local_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    environment_name: str,
    environment_value: str,
) -> None:
    provider = tracing.TracerProvider()
    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setenv(environment_name, environment_value)

    with caplog.at_level("ERROR", logger="kensa.tracing"):
        kensa.instrument(
            tmp_path,
            otlp_endpoint="http://127.0.0.1:4318/v1/traces",
        )
        with provider.get_tracer("test").start_as_current_span("local-after-invalid-otlp"):
            pass
    provider.shutdown()

    rows = [json.loads(line) for line in (tmp_path / "spans.jsonl").read_text().splitlines()]
    assert rows[-1]["name"] == "local-after-invalid-otlp"
    assert "OTLP HTTP exporter configuration is invalid" in caplog.text


def test_span_processor_registration_can_retry_after_factory_failure() -> None:
    class Provider:
        def __init__(self) -> None:
            self.processors: list[Any] = []

        def add_span_processor(self, processor: Any) -> None:
            self.processors.append(processor)

    provider = Provider()

    def fail() -> Any:
        raise RuntimeError("factory failed")

    with pytest.raises(RuntimeError, match="factory failed"):
        tracing._add_span_processors_once(provider, [("test", "retry", fail)])

    processor = object()
    assert tracing._add_span_processors_once(
        provider,
        [("test", "retry", lambda: cast(Any, processor))],
    )
    assert provider.processors == [processor]


def test_span_processor_registration_does_not_retain_provider() -> None:
    class Provider:
        def add_span_processor(self, processor: Any) -> None:
            del processor

    provider = Provider()
    reference = weakref.ref(provider)
    assert tracing._add_span_processors_once(
        provider,
        [("test", "weak", lambda: cast(Any, object()))],
    )

    del provider
    gc.collect()

    assert reference() is None


def test_instrument_attaches_to_active_provider_when_set_silently_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProviderWithoutProcessor:
        pass

    class ActiveProvider:
        def __init__(self) -> None:
            self.processors: list[Any] = []

        def add_span_processor(self, processor: Any) -> None:
            self.processors.append(processor)

    active = ActiveProvider()
    providers = iter([ProviderWithoutProcessor(), active])
    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: next(providers))
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", lambda provider: None)

    kensa.instrument(tmp_path)

    assert len(active.processors) == 1


def test_instrument_reports_when_no_provider_accepts_processors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ProviderWithoutProcessor:
        pass

    provider = ProviderWithoutProcessor()
    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", lambda provider: None)
    monkeypatch.setattr(
        tracing,
        "_otlp_span_exporter_class",
        lambda: pytest.fail("unused OTLP exporter must not be constructed"),
    )

    with caplog.at_level("ERROR", logger="kensa.tracing"):
        kensa.instrument(
            tmp_path,
            otlp_endpoint="http://127.0.0.1:4318/v1/traces",
        )

    assert "exporters were not installed" in caplog.text
    assert not (tmp_path / "spans.jsonl").exists()


def test_otlp_timeout_honors_explicit_and_standard_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert tracing._resolved_otlp_timeout(4.0) == 4.0
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "7")
    assert tracing._resolved_otlp_timeout(None) is None
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TIMEOUT")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "8")
    assert tracing._resolved_otlp_timeout(None) is None


def test_instrument_exports_finished_otel_spans_to_jsonl(tmp_path: Path) -> None:
    kensa.instrument(tmp_path)

    with record_tool_call("lookup_customer", **{"kensa.cost_usd": 0.01}):
        pass

    spans_path = tmp_path / "spans.jsonl"
    rows = [json.loads(line) for line in spans_path.read_text().splitlines()]

    assert rows
    assert rows[-1]["name"] == "lookup_customer"
    assert rows[-1]["attributes"]["kensa.tool.name"] == "lookup_customer"
    assert rows[-1]["trace_id"]
    assert "resource_attributes" in rows[-1]
    assert "instrumentation_scope" in rows[-1]
    assert rows[-1]["events"] == []
    assert rows[-1]["links"] == []


def test_record_tool_call_captures_precomputed_and_in_context_results(tmp_path: Path) -> None:
    kensa.instrument(tmp_path)
    arguments = {"customer": {"id": "cus_1"}}
    result = {"found": True}

    with record_tool_call("lookup_customer", arguments=arguments) as tool_call:
        tool_call.set_result(result)
        arguments["customer"]["id"] = "mutated"
        result["found"] = False
    with record_tool_call("explicit_null", arguments=None, result=None):
        pass

    rows = [json.loads(line) for line in (tmp_path / "spans.jsonl").read_text().splitlines()]
    captured = rows[-2]["attributes"]
    explicit_null = rows[-1]["attributes"]
    assert json.loads(captured["kensa.tool.args"]) == {"customer": {"id": "cus_1"}}
    assert json.loads(captured["kensa.tool.result"]) == {"found": True}
    assert json.loads(explicit_null["kensa.tool.args"]) is None
    assert json.loads(explicit_null["kensa.tool.result"]) is None


def test_record_tool_call_rejects_duplicate_result_capture(tmp_path: Path) -> None:
    kensa.instrument(tmp_path)

    with (
        record_tool_call("precomputed", result={"first": True}) as tool_call,
        pytest.raises(RuntimeError, match="already recorded"),
    ):
        tool_call.set_result({"second": True})
    with record_tool_call("in_context") as tool_call:
        tool_call.set_result({"first": True})
        with pytest.raises(RuntimeError, match="already recorded"):
            tool_call.set_result({"second": True})

    rows = [json.loads(line) for line in (tmp_path / "spans.jsonl").read_text().splitlines()]
    assert json.loads(rows[-2]["attributes"]["kensa.tool.result"]) == {"first": True}
    assert json.loads(rows[-1]["attributes"]["kensa.tool.result"]) == {"first": True}


@pytest.mark.parametrize(
    ("boundary", "payload"),
    [
        ("arguments", object()),
        ("arguments", {"value": math.nan}),
        ("result", object()),
        ("result", {"value": math.inf}),
    ],
)
def test_record_tool_call_rejects_invalid_initial_payloads(
    boundary: str,
    payload: Any,
) -> None:
    kwargs = {boundary: payload}

    with (
        pytest.raises(TypeError, match=boundary),
        record_tool_call("invalid", **kwargs),
    ):
        pytest.fail("invalid payload entered the tool body")


def test_record_tool_call_rejects_invalid_set_result_without_lossy_evidence(
    tmp_path: Path,
) -> None:
    kensa.instrument(tmp_path)

    def capture_invalid_result() -> None:
        with record_tool_call("invalid_result", arguments={"safe": True}) as tool_call:
            tool_call.set_result(object())

    with pytest.raises(TypeError, match="set_result"):
        capture_invalid_result()

    row = json.loads((tmp_path / "spans.jsonl").read_text().splitlines()[-1])
    assert row["status"] == "error"
    assert "kensa.tool.result" not in row["attributes"]
    assert "object at" not in json.dumps(row["attributes"])


def test_record_llm_call_exports_llm_span(tmp_path: Path) -> None:
    kensa.instrument(tmp_path)

    with record_llm_call("openai.responses.create", provider="openai", model="gpt-5-mini"):
        pass

    rows = [json.loads(line) for line in (tmp_path / "spans.jsonl").read_text().splitlines()]
    row = rows[-1]
    assert row["name"] == "openai.responses.create"
    assert row["attributes"]["kensa.span.kind"] == "llm"
    assert row["attributes"]["gen_ai.operation.name"] == "chat"
    assert row["attributes"]["kensa.llm.provider"] == "openai"
    assert row["attributes"]["gen_ai.provider.name"] == "openai"
    assert "gen_ai.system" not in row["attributes"]
    assert row["attributes"]["kensa.llm.model"] == "gpt-5-mini"
    assert row["attributes"]["gen_ai.request.model"] == "gpt-5-mini"


def test_record_llm_call_uses_client_span_kind_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kinds: list[SpanKind] = []

    class Tracer:
        def start_as_current_span(self, name: str, **kwargs: Any) -> Any:
            del name
            kinds.append(kwargs["kind"])
            return nullcontext()

    monkeypatch.setattr(tracing.trace, "get_tracer", lambda name: Tracer())

    with record_llm_call():
        pass
    with record_llm_call(span_kind=SpanKind.INTERNAL):
        pass

    assert kinds == [SpanKind.CLIENT, SpanKind.INTERNAL]


def test_record_llm_call_uses_requested_genai_operation_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation_names: list[str] = []

    class Tracer:
        def start_as_current_span(self, name: str, **kwargs: Any) -> Any:
            del name
            operation_names.append(kwargs["attributes"]["gen_ai.operation.name"])
            return nullcontext()

    monkeypatch.setattr(tracing.trace, "get_tracer", lambda name: Tracer())

    with record_llm_call(operation_name="embeddings"):
        pass
    with record_llm_call(operation_name="text_completion"):
        pass

    assert operation_names == ["embeddings", "text_completion"]


def test_record_span_flattens_explicit_attributes(tmp_path: Path) -> None:
    kensa.instrument(tmp_path)

    with record_span("nested", attributes={"attempt": 1}):
        pass
    with record_span("scalar", attributes="value"):
        pass

    rows = [json.loads(line) for line in (tmp_path / "spans.jsonl").read_text().splitlines()]
    assert rows[-2]["attributes"] == {"attempt": 1}
    assert rows[-1]["attributes"] == {"attributes": "value"}


def test_instrument_run_directory_writes_manifest(tmp_path: Path) -> None:
    kensa.instrument(tmp_path, run_id="local-1", service_name="agent")

    with record_tool_call("lookup_customer"):
        pass

    run_dir = tmp_path / "runs" / "local-1"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert (run_dir / "spans.jsonl").exists()
    assert manifest["schema_version"] == "kensa.trace_manifest.v1"
    assert manifest["run_id"] == "local-1"
    assert manifest["service_name"] == "agent"
    assert manifest["span_count"] >= 1
    assert manifest["trace_count"] >= 1


def test_trace_cli_samples_exported_otel_span_file(tmp_path: Path, capsys) -> None:
    source = tmp_path / "spans.jsonl"
    source.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "redaction": {
                    "version": "kensa.redactor.v2",
                    "mandatory": True,
                    "language": "en",
                    "value_redaction_applied": True,
                    "redaction_available": True,
                    "ruleset_hash": redact.RULESET_HASH,
                    "pseudonymization": "instance-counter",
                    "model": {
                        "name": "en_core_web_sm",
                        "version": "3.8.0",
                        "checksum_verified": True,
                    },
                }
            }
        )
    )
    source.write_text(
        json.dumps(
            {
                "schema_version": "kensa.trace_view.v2",
                "id": "tr_1",
                "name": "lookup_customer",
                "source": {
                    "provider": "local-jsonl",
                    "import_run_id": "import",
                    "imported_at": "2026-06-30T00:00:00Z",
                },
                "started_at_unix_nano": None,
                "ended_at_unix_nano": None,
                "duration_ms": 0.0,
                "status": "ok",
                "input": None,
                "output": None,
                "spans": [
                    {
                        "id": "sp_1",
                        "trace_id": "tr_1",
                        "parent_id": None,
                        "name": "lookup_customer",
                        "kind": "tool",
                        "tool_name": "lookup_customer",
                        "started_at_unix_nano": None,
                        "ended_at_unix_nano": None,
                        "duration_ms": 0.0,
                        "status": "ok",
                        "status_message": None,
                        "input": None,
                        "output": None,
                        "usage": {
                            "model_provider": None,
                            "model": None,
                            "input_tokens": None,
                            "output_tokens": None,
                            "total_tokens": None,
                            "cache_read_input_tokens": None,
                            "cache_creation_input_tokens": None,
                            "cost_usd": None,
                        },
                    }
                ],
            }
        )
        + "\n"
    )
    manifest_path = source.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text())
    manifest["artifact_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))

    assert (
        cli_traces.cmd_traces(
            SimpleNamespace(traces_command="sample", source=str(source), json=False)
        )
        == 0
    )

    sample = json.loads(capsys.readouterr().out)
    assert sample["id"] == "tr_1"
    assert sample["spans"][0]["tool_name"] == "lookup_customer"
