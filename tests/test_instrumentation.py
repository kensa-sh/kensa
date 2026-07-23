from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.trace import SpanKind

import kensa
from kensa import cli_traces, redact, tracing
from kensa.tracing import record_llm_call, record_span, record_tool_call


def test_instrument_noops_without_trace_dir(monkeypatch) -> None:
    monkeypatch.delenv("KENSA_TRACE_DIR", raising=False)
    monkeypatch.setattr(
        tracing,
        "_add_jsonl_processor",
        lambda *args, **kwargs: pytest.fail("instrument should no-op"),
    )

    kensa.instrument()


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
