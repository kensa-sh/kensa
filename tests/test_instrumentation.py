from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import kensa
from kensa import cli_traces, tracing
from kensa.tracing import record_llm_call, record_tool_call


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
    assert row["attributes"]["kensa.llm.provider"] == "openai"
    assert row["attributes"]["gen_ai.system"] == "openai"
    assert row["attributes"]["kensa.llm.model"] == "gpt-5-mini"
    assert row["attributes"]["gen_ai.request.model"] == "gpt-5-mini"


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
    source.write_text(
        json.dumps(
            {
                "schema_version": "kensa.trace_view.v1",
                "id": "tr_1",
                "name": "lookup_customer",
                "source": {
                    "provider": "local-jsonl",
                    "import_run_id": "import",
                    "imported_at": "2026-06-30T00:00:00Z",
                    "source_path": "spans.jsonl",
                    "source_url": None,
                    "trace_url": None,
                },
                "started_at_unix_nano": None,
                "ended_at_unix_nano": None,
                "duration_ms": 0.0,
                "status": "ok",
                "input": None,
                "output": None,
                "attributes": {},
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
                        "attributes": {"kensa.tool.name": "lookup_customer"},
                        "events": [],
                        "raw": None,
                    }
                ],
                "raw": None,
            }
        )
        + "\n"
    )

    assert (
        cli_traces.cmd_traces(
            SimpleNamespace(traces_command="sample", source=str(source), json=False)
        )
        == 0
    )

    sample = json.loads(capsys.readouterr().out)
    assert sample["id"] == "tr_1"
    assert sample["spans"][0]["tool_name"] == "lookup_customer"
