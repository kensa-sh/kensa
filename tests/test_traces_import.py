from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import FakeRedactionEnv

from kensa import redact
from kensa import traces as traces_module
from kensa.redact import (
    RULESET_HASH,
    RedactionError,
    RedactionGateError,
    RedactionNotReadyError,
)
from kensa.traces import (
    import_redaction_manifest,
    import_trace_records,
    import_trace_source,
    load_trace_views,
    trace_view_summary,
    write_trace_manifest,
)

TRACE_VIEW_KEYS = {
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
}
TRACE_SOURCE_KEYS = {
    "provider",
    "import_run_id",
    "imported_at",
    "source_path",
    "source_url",
    "trace_url",
}
SPAN_VIEW_KEYS = {
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
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_safe_sibling_manifest(artifact: Path) -> Path:
    manifest_path = artifact.with_suffix(".manifest.json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "kensa.trace_import_manifest.v1",
                "artifact_sha256": hashlib.sha256(
                    artifact.read_bytes() if artifact.exists() else b""
                ).hexdigest(),
                "redaction": {
                    "version": "kensa.redactor.v2",
                    "mandatory": True,
                    "language": "en",
                    "value_redaction_applied": True,
                    "redaction_available": True,
                    "ruleset_hash": RULESET_HASH,
                    "pseudonymization": "instance-counter",
                    "model": {
                        "name": "en_core_web_sm",
                        "version": "3.8.0",
                        "checksum_verified": True,
                    },
                },
            }
        )
    )
    return manifest_path


def _assert_trace_view_shape(trace: dict[str, Any]) -> None:
    assert set(trace) == TRACE_VIEW_KEYS
    assert trace["schema_version"] == traces_module.TRACE_VIEW_SCHEMA_VERSION
    assert set(trace["source"]) == TRACE_SOURCE_KEYS
    for span in trace["spans"]:
        assert set(span) == SPAN_VIEW_KEYS


def _minimal_trace_view(trace_id: str = "tr_1") -> dict[str, Any]:
    return {
        "schema_version": "kensa.trace_view.v1",
        "id": trace_id,
        "name": None,
        "source": {
            "provider": "jsonl",
            "import_run_id": "import",
            "imported_at": "2026-06-30T00:00:00Z",
            "source_path": "traces.jsonl",
            "source_url": None,
            "trace_url": None,
        },
        "started_at_unix_nano": None,
        "ended_at_unix_nano": None,
        "duration_ms": 0.0,
        "status": "unknown",
        "input": None,
        "output": None,
        "attributes": {},
        "spans": [],
        "raw": None,
    }


def test_trace_manifest_round_trip(tmp_path: Path) -> None:
    run_dir = tmp_path / ".kensa" / "traces" / "runs" / "local-1"

    manifest = write_trace_manifest(
        run_dir,
        run_id="local-1",
        source="local-jsonl",
        service_name="agent",
        span_count=3,
        trace_count=2,
        created_at="2026-05-18T12:00:00Z",
    )

    assert manifest.to_dict()["schema_version"] == "kensa.trace_manifest.v1"
    payload = json.loads((run_dir / "manifest.json").read_text())
    assert payload["span_count"] == 3
    # Runtime trial telemetry is marked as raw source data, never mode-based.
    assert payload["redaction"]["raw_source"] is True
    assert payload["redaction"]["mandatory"] is False
    assert payload["redaction"]["value_redaction_applied"] is False
    assert "kensa import" in payload["redaction"]["note"]
    # Exposure gates always treat runtime trial manifests as unsafe.
    from kensa.redact import safe_manifest

    assert safe_manifest(payload["redaction"]) is False


def test_import_jsonl_records_write_trace_views_and_manifest(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_text(
        json.dumps(
            {
                "id": "tr_1",
                "name": "refund",
                "input": "hello",
                "api_key": "secret",
                "started_at_unix_nano": 1_000_000,
                "ended_at_unix_nano": 3_500_000,
            }
        )
        + "\n"
        + json.dumps({"id": "tr_2", "input": "ignored"})
        + "\n"
    )
    out = tmp_path / "imports" / "json.jsonl"

    result = import_trace_source(
        provider="jsonl",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
    )

    rows = _read_jsonl(out)
    row = rows[0]
    _assert_trace_view_shape(row)
    assert result.records_written == 1
    assert result.span_count == 0
    assert result.manifest_path == out.with_suffix(".manifest.json")
    assert result.warnings == [
        "secret-like fields were redacted",
        "mandatory value redaction changed 2 value(s)",
    ]
    assert row["id"] == "tr_1"
    assert row["name"] == "refund"
    assert row["duration_ms"] == 2.5
    assert row["status"] == "unknown"
    assert row["input"] == "hello"
    assert row["output"] is None
    assert row["attributes"] == {"api_key": "[SECRET_1]"}
    assert row["raw"]["api_key"] == "[SECRET_1]"
    assert row["source"]["provider"] == "jsonl"
    assert row["source"]["source_path"] is None
    assert row["source"]["source_url"] is None
    assert result.manifest_path is not None
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["artifact_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert {"source", "project", "since", "endpoint"}.isdisjoint(manifest)
    assert manifest["records_written"] == 1
    assert manifest["trace_count"] == 1
    assert manifest["span_count"] == 0
    redaction = manifest["redaction"]
    assert redaction["version"] == "kensa.redactor.v2"
    assert redaction["mandatory"] is True
    assert redaction["value_redaction_applied"] is True
    assert redaction["redaction_available"] is True
    assert redaction["secret_keys_redacted"] is True
    assert redaction["changed_value_count"] == 2
    assert redaction["pseudonymization"] == "instance-counter"
    assert redaction["model"]["name"] == "en_core_web_sm"
    # The gated load path accepts the freshly written artifact.
    assert load_trace_views(out) == rows


def test_import_json_records_spans_without_synthetic_semantics(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    source = tmp_path / "trace-spans.json"
    source.write_text(
        json.dumps(
            {
                "traces": [
                    {
                        "id": "trace_1",
                        "input": "shared input",
                        "output": "shared output",
                        "spans": [
                            {
                                "span_id": "span_1",
                                "name": "lookup",
                                "status": "ERROR",
                                "attributes": {"tool.name": "lookup_customer"},
                            }
                        ],
                    }
                ]
            }
        )
    )
    out = tmp_path / "imports" / "spans.jsonl"

    result = import_trace_source(
        provider="json",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
    )

    row = _read_jsonl(out)[0]
    span = row["spans"][0]
    _assert_trace_view_shape(row)
    assert result.records_written == 1
    assert result.span_count == 1
    assert row["id"] == "trace_1"
    assert row["status"] == "error"
    assert row["input"] == "shared input"
    assert row["output"] == "shared output"
    assert span["id"] == "span_1"
    assert span["trace_id"] == "trace_1"
    assert span["kind"] == "tool"
    assert span["tool_name"] == "lookup_customer"
    assert span["input"] is None
    assert span["output"] is None
    assert "kensa.case.input" not in span["attributes"]
    assert "kensa.final_output" not in span["attributes"]


def test_import_jsonl_span_rows_group_by_trace_and_local_manifest(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    run_dir = tmp_path / ".kensa" / "traces" / "runs" / "local"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps({"schema_version": "kensa.trace_manifest.v1", "source": "local-jsonl"})
    )
    source = run_dir / "spans.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "trace_id": "tr_1",
                        "span_id": "span_1",
                        "name": "first",
                        "start_time_unix_nano": 1_000,
                        "end_time_unix_nano": 2_000,
                    }
                ),
                json.dumps(
                    {
                        "trace_id": "tr_1",
                        "span_id": "span_2",
                        "name": "second",
                        "status": "ok",
                    }
                ),
                json.dumps({"trace_id": "tr_2", "span_id": "span_3", "name": "third"}),
            ]
        )
        + "\n"
    )
    out = tmp_path / "imports" / "grouped.jsonl"

    result = import_trace_source(
        provider="jsonl",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
    )

    row = _read_jsonl(out)[0]
    assert result.provider == "local-jsonl"
    assert result.records_written == 1
    assert result.span_count == 2
    assert row["id"] == "tr_1"
    assert row["source"]["provider"] == "local-jsonl"
    assert row["input"] is None
    assert row["output"] is None
    assert [span["id"] for span in row["spans"]] == ["span_1", "span_2"]
    assert row["duration_ms"] == 0.001


def test_import_requires_redaction_readiness_before_reading_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        redact,
        "missing_redaction_dependencies",
        lambda: redact.REDACTION_EXTRA_MODULES,
    )
    source = tmp_path / "traces.jsonl"
    source.write_text(json.dumps({"id": "tr_1", "input": "hello"}) + "\n")
    out = tmp_path / "imports" / "blocked.jsonl"

    with pytest.raises(RedactionNotReadyError, match="Install kensa"):
        import_trace_source(
            provider="jsonl",
            source=str(source),
            out=out,
            limit=1,
            max_payload_bytes=source.stat().st_size,
        )
    assert not out.exists()


def test_import_redacts_values_with_stable_instance_aliases(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_text(
        json.dumps(
            {
                "id": "tr_1",
                "input": "Ask Alice at alice@example.com",
                "output": "Alice replied",
                "spans": [
                    {
                        "span_id": "span_1",
                        "status_message": "Alice failed after emailing alice@example.com",
                        "attributes": {"detail": "Alice again"},
                        "events": [{"attributes": {"exception.message": "Alice exploded"}}],
                    }
                ],
            }
        )
        + "\n"
    )
    out = tmp_path / "imports" / "aliases.jsonl"

    result = import_trace_source(
        provider="jsonl",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
    )

    row = _read_jsonl(out)[0]
    span = row["spans"][0]
    # One value keeps one alias across input, output, and span fields (AC 48).
    assert row["input"] == "Ask [PERSON_1] at [EMAIL_ADDRESS_1]"
    assert row["output"] == "[PERSON_1] replied"
    assert span["status_message"] == "[PERSON_1] failed after emailing [EMAIL_ADDRESS_1]"
    assert span["attributes"]["detail"] == "[PERSON_1] again"
    assert span["events"][0]["attributes"]["exception.message"] == "[PERSON_1] exploded"
    assert span["raw"]["status_message"] == ("[PERSON_1] failed after emailing [EMAIL_ADDRESS_1]")
    assert result.redaction["entity_instance_counts"]["PERSON"] == 1
    assert result.redaction["entity_instance_counts"]["EMAIL_ADDRESS"] == 1
    assert "mandatory value redaction changed" in result.warnings[0]
    # Alias determinism: re-importing identical input yields identical aliases.
    rerun_out = tmp_path / "imports" / "aliases-rerun.jsonl"
    import_trace_source(
        provider="jsonl",
        source=str(source),
        out=rerun_out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
    )
    assert _read_jsonl(rerun_out) == [row]
    # The value-to-alias map is never persisted anywhere in the artifact or manifest.
    assert result.manifest_path is not None
    persisted = out.read_text() + result.manifest_path.read_text()
    assert "Alice" not in persisted
    assert "alice@example.com" not in persisted


def test_import_detect_secrets_hits_redact_whole_values(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_text(json.dumps({"id": "tr_1", "input": "tok_live"}) + "\n")
    out = tmp_path / "imports" / "detect-secrets.jsonl"

    result = import_trace_source(
        provider="jsonl",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
    )

    row = _read_jsonl(out)[0]
    assert row["input"] == "[SECRET_1]"
    assert row["raw"]["input"] == "[SECRET_1]"
    assert result.redaction["redacted_span_count"] >= 2


def test_import_fails_closed_and_writes_nothing_on_redaction_errors(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_text(json.dumps({"id": "tr_1", "input": "Alice"}) + "\n")
    out = tmp_path / "imports" / "failing.jsonl"
    redaction_ready.analyzer_error = RuntimeError("model exploded")

    with pytest.raises(RedactionError, match="value redaction failed"):
        import_trace_source(
            provider="jsonl",
            source=str(source),
            out=out,
            limit=1,
            max_payload_bytes=source.stat().st_size,
        )
    assert not out.exists()
    assert not out.with_suffix(".manifest.json").exists()


def test_import_trace_records_shares_the_redaction_pipeline(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    payload = {
        "traces": [
            {
                "id": "trace_1",
                "input": "Alice needs help",
                "observations": [{"id": "obs_1", "type": "TOOL", "input": {"query": "Alice"}}],
            }
        ]
    }
    out = tmp_path / "imports" / "connected.jsonl"

    result = import_trace_records(
        provider="langfuse",
        payload=payload,
        source_label="langfuse:connected",
        out=out,
        limit=10,
        max_payload_bytes=10_000,
    )

    row = _read_jsonl(out)[0]
    assert result.provider == "langfuse"
    assert result.source == "langfuse:connected"
    assert result.bytes_read == len(json.dumps(payload, sort_keys=True).encode())
    assert row["input"] == "[PERSON_1] needs help"
    assert row["spans"][0]["input"] == {"query": "[PERSON_1]"}
    assert row["source"]["source_path"] is None
    assert result.redaction["version"] == "kensa.redactor.v2"


def test_import_trace_records_enforces_payload_bound_in_memory(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    payload = {"traces": [{"id": "tr_1", "input": "x" * 200}]}
    with pytest.raises(ValueError, match="payload exceeds --max-payload-bytes"):
        import_trace_records(
            provider="langfuse",
            payload=payload,
            source_label="langfuse:connected",
            out=tmp_path / "imports" / "bounded.jsonl",
            limit=10,
            max_payload_bytes=64,
        )
    assert not (tmp_path / "imports" / "bounded.jsonl").exists()


def test_import_trace_records_accepts_json_record_payloads(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    result = import_trace_records(
        provider="jsonl",
        payload=[{"id": "tr_mem", "input": "Alice"}],
        source_label="memory:test",
        out=tmp_path / "imports" / "records.jsonl",
        limit=5,
        max_payload_bytes=10_000,
    )
    row = _read_jsonl(result.out_path)[0]
    assert row["id"] == "tr_mem"
    assert row["input"] == "[PERSON_1]"


def test_load_trace_views_reports_unreadable_artifacts(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "missing-artifact.jsonl"
    _write_safe_sibling_manifest(artifact)
    with pytest.raises(ValueError, match="Could not read trace import artifact"):
        load_trace_views(artifact)


def test_load_trace_views_gates_on_the_sibling_manifest(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    artifact = tmp_path / "artifact.jsonl"
    artifact.write_text(json.dumps(_minimal_trace_view()) + "\n")

    with pytest.raises(RedactionGateError, match="no redaction manifest"):
        load_trace_views(artifact)

    manifest_path = artifact.with_suffix(".manifest.json")
    manifest_path.write_text("{")
    with pytest.raises(RedactionGateError, match="no redaction manifest"):
        load_trace_views(artifact)

    manifest_path.write_text(json.dumps(["not", "a", "dict"]))
    with pytest.raises(RedactionGateError, match="no redaction manifest"):
        load_trace_views(artifact)

    manifest_path.write_text(
        json.dumps({"redaction": {"version": "kensa.redactor.v1", "mode": "strict"}})
    )
    with pytest.raises(RedactionGateError, match=r"kensa\.redactor\.v2"):
        load_trace_views(artifact)

    manifest_path.write_text(json.dumps({"redaction": {"raw_source": True}}))
    with pytest.raises(RedactionGateError, match="raw source telemetry"):
        load_trace_views(artifact)

    _write_safe_sibling_manifest(artifact)
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("artifact_sha256")
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RedactionGateError, match="no valid artifact SHA-256"):
        load_trace_views(artifact)

    _write_safe_sibling_manifest(artifact)
    assert load_trace_views(artifact) == [_minimal_trace_view()]
    assert import_redaction_manifest(artifact)["version"] == "kensa.redactor.v2"


def test_load_trace_views_rejects_artifact_replaced_after_manifest(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_text(json.dumps({"id": "tr_1", "input": "hello"}) + "\n")
    artifact = tmp_path / "imports" / "traces.jsonl"
    import_trace_source(
        provider="jsonl",
        source=str(source),
        out=artifact,
        limit=1,
        max_payload_bytes=source.stat().st_size,
    )

    replacement = _minimal_trace_view()
    replacement["input"] = "alice@example.com"
    artifact.write_text(json.dumps(replacement) + "\n")

    with pytest.raises(RedactionGateError, match="does not match its redaction manifest"):
        load_trace_views(artifact)


def test_import_sanitizes_trace_urls_with_safe_endpoint(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_text(
        json.dumps(
            {
                "id": "tr_1",
                "trace_url": "https://user:pw@trace.example.com/v1/api-token/tr_1",
            }
        )
        + "\n"
    )
    out = tmp_path / "imports" / "trace-url.jsonl"

    import_trace_source(
        provider="jsonl",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
    )

    row = _read_jsonl(out)[0]
    assert row["source"]["trace_url"] == "https://trace.example.com/v1/[redacted]/tr_1"


@pytest.mark.parametrize("provider", ["json", "jsonl"])
def test_import_json_trace_records_accept_trace_id_without_id(
    provider: str,
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    source = tmp_path / f"trace.{provider}"
    row = {"trace_id": "tr_1", "input": "hello"}
    if provider == "json":
        source.write_text(json.dumps(row))
    else:
        source.write_text(json.dumps(row) + "\n")
    out = tmp_path / "imports" / "trace.jsonl"

    result = import_trace_source(
        provider=provider,
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
    )

    imported = _read_jsonl(out)[0]
    assert result.records_written == 1
    assert result.span_count == 0
    assert imported["id"] == "tr_1"
    assert imported["input"] == "hello"
    assert imported["spans"] == []


def test_import_otlp_records_groups_spans_into_trace_view(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    source = tmp_path / "otlp.json"
    source.write_text(
        json.dumps(
            {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [
                                {"key": "service.name", "value": {"stringValue": "agent"}}
                            ]
                        },
                        "scopeSpans": [
                            {
                                "scope": {
                                    "name": "scope",
                                    "version": "1.0",
                                    "attributes": [
                                        {
                                            "key": "array",
                                            "value": {
                                                "arrayValue": {"values": [{"stringValue": "a"}]}
                                            },
                                        },
                                        {
                                            "key": "kv",
                                            "value": {
                                                "kvlistValue": {
                                                    "values": [
                                                        {
                                                            "key": "nested",
                                                            "value": {"intValue": "1"},
                                                        }
                                                    ]
                                                }
                                            },
                                        },
                                    ],
                                },
                                "spans": [
                                    {
                                        "traceId": "abc",
                                        "spanId": "def",
                                        "parentSpanId": "",
                                        "name": "lookup_customer",
                                        "startTimeUnixNano": "bad",
                                        "endTimeUnixNano": "2",
                                        "traceState": "vendor=value",
                                        "attributes": [
                                            {
                                                "key": "kensa.tool.name",
                                                "value": {"stringValue": "lookup_customer"},
                                            },
                                            {
                                                "key": "authorization",
                                                "value": {"stringValue": "Bearer secret"},
                                            },
                                            {"key": "raw", "value": "plain"},
                                            {"key": "unknown", "value": {"other": "value"}},
                                        ],
                                        "events": [
                                            {
                                                "name": "exception",
                                                "timeUnixNano": "3",
                                                "attributes": [
                                                    {
                                                        "key": "exception.message",
                                                        "value": {"stringValue": "boom"},
                                                    }
                                                ],
                                            }
                                        ],
                                        "links": [
                                            {
                                                "traceId": "linked",
                                                "spanId": "span",
                                                "traceState": "state",
                                            }
                                        ],
                                        "status": {
                                            "code": 2,
                                            "message": "failed",
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )
    out = tmp_path / "otlp.jsonl"

    result = import_trace_source(
        provider="otlp",
        source=str(source),
        out=out,
        limit=10,
        max_payload_bytes=source.stat().st_size,
    )

    row = _read_jsonl(out)[0]
    span = row["spans"][0]
    _assert_trace_view_shape(row)
    assert result.records_written == 1
    assert result.span_count == 1
    assert row["id"] == "abc"
    assert row["status"] == "error"
    assert row["started_at_unix_nano"] is None
    assert row["attributes"]["resource_attributes"] == {"service.name": "agent"}
    assert row["attributes"]["instrumentation_scope"]["name"] == "scope"
    assert row["attributes"]["instrumentation_scope"]["attributes"] == {
        "array": ["a"],
        "kv": {"nested": "1"},
    }
    assert span["id"] == "def"
    assert span["status"] == "error"
    assert span["events"][0]["attributes"]["exception.message"] == "boom"
    assert span["attributes"]["links"][0]["trace_id"] == "linked"
    assert span["attributes"]["authorization"] == "[SECRET_1]"
    assert span["attributes"]["raw"] == "plain"
    assert span["attributes"]["unknown"] == {"other": "value"}

    source.write_text(
        json.dumps(
            {"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "x", "spanId": "y"}]}]}]}
        )
    )
    import_trace_source(
        provider="otlp",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
    )
    row = _read_jsonl(out)[0]
    assert row["status"] == "unknown"

    source.write_text(json.dumps({"resourceSpans": {}}))
    empty = import_trace_source(
        provider="otlp",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
    )
    assert empty.records_written == 0
    assert out.read_text() == ""


def test_import_langfuse_records_preserve_trace_and_observation_fields(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    source = tmp_path / "langfuse.json"
    source.write_text(
        json.dumps(
            {
                "traces": [
                    {
                        "id": "trace_1",
                        "name": "agent",
                        "user_id": "user_1",
                        "session_id": "session_1",
                        "release": "2026.05.21",
                        "version": "1.2.3",
                        "environment": "staging",
                        "input": {"input": "Refund me"},
                        "output": {"output": "Refunded"},
                        "metadata": {"tenant": "support"},
                        "feedback": {"thumbs": "down"},
                        "scores": [
                            {
                                "name": "helpfulness",
                                "value": 0.1,
                                "comment": "unsafe refund",
                            }
                        ],
                        "observations": [
                            {
                                "id": "obs_1",
                                "traceId": "trace_1",
                                "parentObservationId": "trace_1",
                                "name": "issue_refund",
                                "type": "TOOL",
                                "input": {"charge": "ch_1"},
                                "output": {"ok": True},
                                "level": "ERROR",
                            }
                        ],
                    }
                ],
                "observations": [
                    {
                        "id": "obs_2",
                        "traceId": "trace_1",
                        "name": "summarize",
                        "type": "GENERATION",
                    }
                ],
            }
        )
    )
    out = tmp_path / "langfuse.jsonl"

    result = import_trace_source(
        provider="langfuse",
        source=str(source),
        out=out,
        limit=10,
        max_payload_bytes=source.stat().st_size,
    )

    rows = _read_jsonl(out)
    row = rows[0]
    _assert_trace_view_shape(row)
    assert result.records_written == 1
    assert result.span_count == 2
    assert row["id"] == "trace_1"
    assert row["input"] == {"input": "Refund me"}
    assert row["output"] == {"output": "Refunded"}
    assert row["attributes"]["tenant"] == "support"
    assert row["attributes"]["user_id"] == "user_1"
    assert row["attributes"]["session_id"] == "session_1"
    assert row["attributes"]["release"] == "2026.05.21"
    assert row["attributes"]["version"] == "1.2.3"
    assert row["attributes"]["environment"] == "staging"
    assert row["attributes"]["feedback"] == {"thumbs": "down"}
    assert row["attributes"]["scores"][0]["value"] == 0.1
    assert row["spans"][0]["kind"] == "tool"
    assert row["spans"][0]["status"] == "error"
    assert row["spans"][0]["tool_name"] == "issue_refund"
    assert row["spans"][0]["input"] == {"charge": "ch_1"}
    assert row["spans"][0]["output"] == {"ok": True}
    assert row["spans"][1]["name"] == "summarize"
    assert row["spans"][1]["kind"] == "llm"
    manifest = json.loads(out.with_suffix(".manifest.json").read_text())
    assert {"source", "project", "since", "endpoint"}.isdisjoint(manifest)
    assert manifest["trace_count"] == 1
    assert manifest["span_count"] == 2

    limited = import_trace_source(
        provider="langfuse",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
    )
    assert limited.records_written == 1
    assert limited.span_count == 2
    trace_only_source = tmp_path / "langfuse-trace-only.json"
    trace_only_source.write_text(json.dumps({"traces": [{"id": "trace_only"}]}))
    trace_only = import_trace_source(
        provider="langfuse",
        source=str(trace_only_source),
        out=out,
        limit=1,
        max_payload_bytes=trace_only_source.stat().st_size,
    )
    assert trace_only.records_written == 1
    trace_only_row = _read_jsonl(out)[0]
    assert trace_only_row["input"] is None
    assert trace_only_row["output"] is None
    assert trace_only_row["spans"] == []


def test_import_langfuse_records_accepts_official_data_envelope(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    source = tmp_path / "langfuse-observations.json"
    source.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "id": "obs_1",
                        "traceId": "trace_1",
                        "traceName": "refund-agent",
                        "traceUserId": "user_1",
                        "traceSessionId": "session_1",
                        "userId": "trace-user-camel",
                        "sessionId": "trace-session-camel",
                        "release": "2026.07.08",
                        "environment": "production",
                        "tags": ["refunds"],
                        "name": "agent",
                        "type": "SPAN",
                        "input": {"message": "Refund me"},
                        "output": {"message": "Done"},
                        "metadata": {"tenant": "support"},
                        "level": "DEFAULT",
                    },
                    {
                        "id": "obs_2",
                        "traceId": "trace_1",
                        "parentObservationId": "obs_1",
                        "name": "lookup_customer",
                        "type": "TOOL",
                        "level": "ERROR",
                    },
                ],
                "meta": {"cursor": None},
            }
        )
    )
    out = tmp_path / "langfuse-observations.jsonl"

    result = import_trace_source(
        provider="langfuse",
        source=str(source),
        out=out,
        limit=10,
        max_payload_bytes=source.stat().st_size,
    )

    rows = _read_jsonl(out)
    row = rows[0]
    assert result.records_written == 1
    assert result.span_count == 2
    assert row["id"] == "trace_1"
    assert row["name"] == "refund-agent"
    assert row["input"] is None
    assert row["output"] is None
    assert row["attributes"]["traceUserId"] == "user_1"
    assert row["attributes"]["traceSessionId"] == "session_1"
    assert row["attributes"]["userId"] == "trace-user-camel"
    assert row["attributes"]["sessionId"] == "trace-session-camel"
    assert row["attributes"]["release"] == "2026.07.08"
    assert row["attributes"]["environment"] == "production"
    assert row["attributes"]["tags"] == ["refunds"]
    assert row["attributes"]["tenant"] == "support"
    assert row["spans"][0]["id"] == "obs_1"
    assert row["spans"][0]["input"] == {"message": "Refund me"}
    assert row["spans"][0]["output"] == {"message": "Done"}
    assert row["spans"][1]["parent_id"] == "obs_1"
    assert row["spans"][1]["kind"] == "tool"
    assert row["spans"][1]["status"] == "error"

    limited = import_trace_source(
        provider="langfuse",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
    )
    assert limited.records_written == 1
    assert limited.span_count == 2


def test_import_langfuse_observation_rows_group_by_trace_id(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    source = tmp_path / "langfuse-observation-groups.json"
    source.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "id": "obs_1",
                        "trace_id": "trace_1",
                        "trace_name": "first",
                        "session_id": "session_1",
                        "start_time": "2026-07-08T00:00:00Z",
                        "end_time": "2026-07-08T00:00:01Z",
                        "observation_type": "SPAN",
                    },
                    {"id": "obs_2", "traceId": "trace_2", "traceName": "second", "type": "SPAN"},
                    {
                        "id": "obs_3",
                        "trace_id": "trace_1",
                        "parent_observation_id": "obs_1",
                        "name": "child",
                        "type": "GENERATION",
                    },
                ],
                "meta": {"cursor": None},
            }
        )
    )
    out = tmp_path / "langfuse-observation-groups.jsonl"

    result = import_trace_source(
        provider="langfuse",
        source=str(source),
        out=out,
        limit=10,
        max_payload_bytes=source.stat().st_size,
    )

    rows = _read_jsonl(out)
    assert result.records_written == 2
    assert result.span_count == 3
    assert [row["id"] for row in rows] == ["trace_1", "trace_2"]
    assert rows[0]["name"] == "first"
    assert rows[0]["attributes"]["trace_name"] == "first"
    assert rows[0]["attributes"]["session_id"] == "session_1"
    assert rows[0]["duration_ms"] == 1000.0
    assert [span["id"] for span in rows[0]["spans"]] == ["obs_1", "obs_3"]
    assert rows[0]["spans"][0]["kind"] == "span"
    assert "end_time" not in rows[0]["spans"][0]["attributes"]
    assert rows[0]["spans"][1]["parent_id"] == "obs_1"
    assert [span["id"] for span in rows[1]["spans"]] == ["obs_2"]


def test_load_trace_views_validates_trace_view_rows_and_summaries(tmp_path: Path) -> None:
    source = tmp_path / "trace-views.jsonl"
    trace = {
        "schema_version": "kensa.trace_view.v1",
        "id": "tr_1",
        "name": None,
        "source": {
            "provider": "jsonl",
            "import_run_id": "import",
            "imported_at": "2026-06-30T00:00:00Z",
            "source_path": "traces.jsonl",
            "source_url": None,
            "trace_url": "https://trace.example/tr_1",
        },
        "started_at_unix_nano": None,
        "ended_at_unix_nano": None,
        "duration_ms": 0.0,
        "status": "ok",
        "input": None,
        "output": None,
        "attributes": {},
        "spans": [],
        "raw": None,
    }
    source.write_text(json.dumps(trace) + "\n")

    _write_safe_sibling_manifest(source)
    rows = load_trace_views(source)

    assert rows == [trace]
    assert trace_view_summary(rows[0]) == {
        "id": "tr_1",
        "name": None,
        "status": "ok",
        "started_at_unix_nano": None,
        "duration_ms": 0.0,
        "span_count": 0,
        "source": {"provider": "jsonl", "trace_url": "https://trace.example/tr_1"},
    }

    old_artifact = tmp_path / "old.jsonl"
    old_artifact.write_text('{"id":"tr_old","spans":[]}\n')
    _write_safe_sibling_manifest(old_artifact)
    with pytest.raises(ValueError, match="Re-import traces with kensa import"):
        load_trace_views(old_artifact)


def test_import_trace_source_validates_bounds_provider_and_mechanical_ids(
    tmp_path: Path,
    redaction_ready: FakeRedactionEnv,
) -> None:
    source = tmp_path / "trace.jsonl"
    source.write_text(json.dumps({"id": "tr_1"}) + "\n")

    with pytest.raises(ValueError, match="--limit"):
        import_trace_source(
            provider="jsonl",
            source=str(source),
            out=tmp_path / "out.jsonl",
            limit=0,
            max_payload_bytes=100,
        )
    with pytest.raises(ValueError, match="--max-payload-bytes"):
        import_trace_source(
            provider="jsonl",
            source=str(source),
            out=tmp_path / "out.jsonl",
            limit=1,
            max_payload_bytes=0,
        )
    with pytest.raises(ValueError, match="source exceeds"):
        import_trace_source(
            provider="jsonl",
            source=str(source),
            out=tmp_path / "out.jsonl",
            limit=1,
            max_payload_bytes=1,
        )
    with pytest.raises(ValueError, match="unsupported"):
        import_trace_source(
            provider="missing",
            source=str(source),
            out=tmp_path / "out.jsonl",
            limit=1,
            max_payload_bytes=100,
        )
    with pytest.raises(ValueError, match="require --source"):
        import_trace_source(
            provider="jsonl",
            source=None,
            out=tmp_path / "out.jsonl",
            limit=1,
            max_payload_bytes=100,
        )
    with pytest.raises(ValueError, match="unsupported"):
        import_trace_source(
            provider="langsmith",
            source=str(source),
            out=tmp_path / "out.jsonl",
            limit=1,
            max_payload_bytes=100,
        )
    no_trace_id = tmp_path / "no-trace-id.jsonl"
    no_trace_id.write_text(json.dumps({"input": "missing"}) + "\n")
    no_trace_out = tmp_path / "no-trace-out.jsonl"
    with pytest.raises(ValueError, match="trace record is missing"):
        import_trace_source(
            provider="jsonl",
            source=str(no_trace_id),
            out=no_trace_out,
            limit=1,
            max_payload_bytes=100,
        )
    assert not no_trace_out.exists()

    no_span_row_id = tmp_path / "no-span-row-id.jsonl"
    no_span_row_id.write_text(json.dumps({"trace_id": "tr_1", "parent_span_id": "parent"}) + "\n")
    no_span_row_out = tmp_path / "no-span-row-out.jsonl"
    with pytest.raises(ValueError, match="span in trace tr_1"):
        import_trace_source(
            provider="jsonl",
            source=str(no_span_row_id),
            out=no_span_row_out,
            limit=1,
            max_payload_bytes=100,
        )
    assert not no_span_row_out.exists()

    mixed_rows = tmp_path / "mixed-traces-and-spans.jsonl"
    mixed_rows.write_text(
        "\n".join(
            [
                json.dumps({"id": "tr_1", "input": "trace"}),
                json.dumps({"trace_id": "tr_1", "span_id": "span_1", "name": "span"}),
            ]
        )
        + "\n"
    )
    mixed_rows_out = tmp_path / "mixed-traces-and-spans-out.jsonl"
    with pytest.raises(ValueError, match="mixed trace records and span rows"):
        import_trace_source(
            provider="jsonl",
            source=str(mixed_rows),
            out=mixed_rows_out,
            limit=10,
            max_payload_bytes=200,
        )
    assert not mixed_rows_out.exists()
    assert (
        traces_module._json_record_is_span_row({"trace_id": "tr_1", "parent_span_id": "parent"})
        is True
    )
    for key in ("span_id", "spanId", "observationId"):
        assert traces_module._json_record_is_span_row({"trace_id": "tr_1", key: None}) is True
    assert traces_module._json_record_is_span_row({"trace_id": "tr_1", "name": "trace"}) is False

    null_span_id = tmp_path / "null-span-id.jsonl"
    null_span_id.write_text(json.dumps({"trace_id": "tr_1", "span_id": None}) + "\n")
    null_span_out = tmp_path / "null-span-out.jsonl"
    with pytest.raises(ValueError, match="span in trace tr_1"):
        import_trace_source(
            provider="jsonl",
            source=str(null_span_id),
            out=null_span_out,
            limit=1,
            max_payload_bytes=100,
        )
    assert not null_span_out.exists()

    bad_spans = tmp_path / "bad-spans.jsonl"
    bad_spans.write_text(json.dumps({"id": "tr_1", "spans": {}}) + "\n")
    bad_spans_out = tmp_path / "bad-spans-out.jsonl"
    with pytest.raises(ValueError, match="spans must be a list"):
        import_trace_source(
            provider="jsonl",
            source=str(bad_spans),
            out=bad_spans_out,
            limit=1,
            max_payload_bytes=100,
        )
    assert not bad_spans_out.exists()

    no_span_id = tmp_path / "no-span-id.json"
    no_span_id.write_text(json.dumps({"id": "tr_1", "spans": [{"name": "missing"}]}))
    no_span_out = tmp_path / "no-span-out.jsonl"
    with pytest.raises(ValueError, match="span in trace tr_1"):
        import_trace_source(
            provider="json",
            source=str(no_span_id),
            out=no_span_out,
            limit=1,
            max_payload_bytes=100,
        )
    assert not no_span_out.exists()

    assert traces_module.safe_endpoint("collector") == "collector"
    assert traces_module.safe_endpoint("https://collector.example.com:bad/path") == (
        "https://collector.example.com/path"
    )
    assert (
        traces_module.safe_endpoint("https://collector.example.com/v1/credential-abc/ingest")
        == "https://collector.example.com/v1/[redacted]/ingest"
    )
    assert traces_module.safe_endpoint("https://collector.example.com") == (
        "https://collector.example.com"
    )
    assert traces_module.safe_endpoint("https://[2001:db8::1]/path") == (
        "https://[2001:db8::1]/path"
    )


def test_atomic_trace_write_preserves_exact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "atomic.jsonl"
    modes: list[str] = []
    named_temporary_file = traces_module.tempfile.NamedTemporaryFile

    def tracking_named_temporary_file(*args: Any, **kwargs: Any) -> Any:
        modes.append(str(args[0]))
        return named_temporary_file(*args, **kwargs)

    monkeypatch.setattr(
        traces_module.tempfile,
        "NamedTemporaryFile",
        tracking_named_temporary_file,
    )
    artifact_bytes = b'{"id":"one"}\n{"id":"two"}\n'

    traces_module._write_bytes_atomic(output, artifact_bytes)

    assert modes == ["wb"]
    assert output.read_bytes() == artifact_bytes


def test_trace_import_internal_edge_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_dir = tmp_path / "bad-manifest"
    manifest_dir.mkdir()
    source = manifest_dir / "spans.jsonl"
    (manifest_dir / "manifest.json").write_text("{")
    assert traces_module._trace_source_provider("jsonl", source) == "jsonl"

    output = tmp_path / "atomic.jsonl"
    original_replace = Path.replace

    def fail_replace(self: Path, target: Path) -> Path:
        if self.name.startswith(f".{output.name}."):
            raise OSError("replace failed")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        traces_module._write_bytes_atomic(output, b"data")
    assert not list(output.parent.glob(f".{output.name}.*"))

    with pytest.raises(ValueError, match="unsupported trace import provider"):
        traces_module._import_trace_views(
            provider="missing",
            data=[],
            limit=1,
            trace_source=traces_module.TraceSource(
                provider="jsonl",
                import_run_id="import",
                imported_at="2026-06-30T00:00:00Z",
                source_path=str(source),
                source_url=None,
                trace_url=None,
            ),
        )

    json_list = tmp_path / "list.json"
    json_list.write_text(json.dumps([{"id": "tr_list"}]))
    assert traces_module._read_json_records(json_list) == [{"id": "tr_list"}]

    blank_jsonl = tmp_path / "blank.jsonl"
    blank_jsonl.write_text("\n" + json.dumps({"id": "tr_1"}) + "\n[]\n")
    assert traces_module._read_json_records(blank_jsonl) == [{"id": "tr_1"}]

    with pytest.raises(ValueError, match="span record is missing"):
        traces_module._required_span_trace_id({"span_id": "span"})
    with pytest.raises(ValueError, match="custom key is missing"):
        traces_module._required_key({}, "custom", label="custom key")

    assert traces_module._unix_nano_or_none(1.5) == 1
    assert traces_module._unix_nano_or_none("  ") is None
    assert traces_module._unix_nano_or_none("2026-06-30T00:00:00") == 1_782_777_600_000_000_000
    assert traces_module._unix_nano_or_none(object()) is None
    assert traces_module._aggregate_status("ok", []) == "ok"
    assert traces_module._status_from_value(True) == "error"
    assert traces_module._status_from_value(2) == "error"
    assert traces_module._status_from_value("2") == "error"
    assert traces_module._status_from_value("STATUS_CODE_ERROR") == "error"
    assert traces_module._status_from_value(" ") == "unknown"
    assert traces_module._status_from_value("maybe") == "unknown"
    ok_span = traces_module.SpanView(
        id="span",
        trace_id="trace",
        parent_id=None,
        name="span",
        kind="span",
        tool_name=None,
        started_at_unix_nano=None,
        ended_at_unix_nano=None,
        duration_ms=0.0,
        status="ok",
        status_message=None,
        input=None,
        output=None,
        attributes={},
        events=[],
        raw=None,
    )
    assert traces_module._aggregate_status("unknown", [ok_span]) == "ok"
    with pytest.raises(ValueError, match="trace record must be a JSON object"):
        traces_module._validate_json_trace_record(cast(dict[str, Any], []))
    with pytest.raises(ValueError, match="span row must be a JSON object"):
        traces_module._validate_json_span_row(cast(dict[str, Any], []))
    assert traces_module._otlp_events([{"timeUnixNano": "bad"}])[0]["time_unix_nano"] is None


def test_load_trace_views_rejects_invalid_trace_view_rows(tmp_path: Path) -> None:
    source = tmp_path / "bad.jsonl"

    def assert_invalid(content: str, match: str = "Re-import traces with kensa import") -> None:
        source.write_text(content)
        _write_safe_sibling_manifest(source)
        with pytest.raises(ValueError, match=match):
            load_trace_views(source)

    assert_invalid("\n{\n")
    assert_invalid("[]\n")

    trace = _minimal_trace_view()
    invalid = dict(trace)
    invalid["extra"] = True
    assert_invalid(json.dumps(invalid) + "\n")

    invalid = dict(trace)
    invalid["source"] = {}
    assert_invalid(json.dumps(invalid) + "\n")

    invalid = dict(trace)
    invalid["spans"] = {}
    assert_invalid(json.dumps(invalid) + "\n")

    invalid = dict(trace)
    invalid["spans"] = [{}]
    assert_invalid(json.dumps(invalid) + "\n")

    invalid = dict(trace)
    invalid["id"] = ""
    assert_invalid(json.dumps(invalid) + "\n", "missing id")

    source.write_bytes(b"\xff")
    _write_safe_sibling_manifest(source)
    with pytest.raises(ValueError, match="Re-import traces with kensa import"):
        load_trace_views(source)
