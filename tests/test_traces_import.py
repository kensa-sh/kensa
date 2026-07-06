from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from kensa import traces as traces_module
from kensa.traces import (
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
    assert json.loads((run_dir / "manifest.json").read_text())["span_count"] == 3
    assert json.loads((run_dir / "manifest.json").read_text())["redaction"]["mode"] == "off"
    assert (
        json.loads((run_dir / "manifest.json").read_text())["redaction"]["secret_keys_redacted"]
        is False
    )


def test_import_jsonl_records_write_trace_views_and_manifest(tmp_path: Path) -> None:
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
        endpoint="https://user:secret@collector.example.com:4318/v1/api-token/ingest?token=x",
    )

    rows = _read_jsonl(out)
    row = rows[0]
    _assert_trace_view_shape(row)
    assert result.records_written == 1
    assert result.span_count == 0
    assert result.manifest_path == out.with_suffix(".manifest.json")
    assert result.warnings == [
        "secret-like fields were redacted",
        "endpoint recorded in import provenance",
    ]
    assert row["id"] == "tr_1"
    assert row["name"] == "refund"
    assert row["duration_ms"] == 2.5
    assert row["status"] == "unknown"
    assert row["input"] == "hello"
    assert row["output"] is None
    assert row["attributes"] == {"api_key": "[redacted]"}
    assert row["raw"]["api_key"] == "[redacted]"
    assert row["source"]["provider"] == "jsonl"
    assert row["source"]["source_path"] == str(source)
    assert row["source"]["source_url"] == (
        "https://collector.example.com:4318/v1/[redacted]/ingest"
    )
    assert result.manifest_path is not None
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["endpoint"] == "https://collector.example.com:4318/v1/[redacted]/ingest"
    assert manifest["records_written"] == 1
    assert manifest["trace_count"] == 1
    assert manifest["span_count"] == 0
    assert manifest["redaction"]["mode"] == "keys"
    assert manifest["redaction"]["requested_mode"] == "keys"
    assert manifest["redaction"]["secret_keys_redacted"] is True
    assert manifest["redaction"]["version"] == traces_module.REDACTOR_VERSION


def test_import_json_records_spans_without_synthetic_semantics(tmp_path: Path) -> None:
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


def test_import_jsonl_span_rows_group_by_trace_and_local_manifest(tmp_path: Path) -> None:
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


def test_import_redaction_off_preserves_values(tmp_path: Path) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_text(json.dumps({"id": "tr_1", "api_key": "secret"}) + "\n")
    out = tmp_path / "imports" / "json.jsonl"

    result = import_trace_source(
        provider="jsonl",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
        endpoint="https://user:secret@collector.example.com/v1/api-token/ingest?token=x",
        redact="off",
    )

    row = _read_jsonl(out)[0]
    assert row["attributes"]["api_key"] == "secret"
    assert row["source"]["source_url"] == (
        "https://user:secret@collector.example.com/v1/api-token/ingest?token=x"
    )
    assert result.warnings == ["endpoint stored verbatim including any credentials (--redact off)"]
    assert result.manifest_path is not None
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["endpoint"] == (
        "https://user:secret@collector.example.com/v1/api-token/ingest?token=x"
    )
    assert manifest["redaction"]["mode"] == "off"
    assert manifest["redaction"]["secret_keys_redacted"] is False


def test_import_strict_redacts_detect_secrets_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_text(json.dumps({"id": "tr_1", "input": "tok_live"}) + "\n")
    out = tmp_path / "imports" / "detect-secrets.jsonl"

    def fake_loader() -> traces_module._StrictValueRedactor:
        return traces_module._StrictValueRedactor(
            detect_secret=lambda value: value == "tok_live",
            redact_pii=lambda value: (value, False),
            dependencies={
                "detect-secrets": "test",
                "en-core-web-sm": "test",
                "presidio-analyzer": "test",
                "spacy": "test",
            },
            presidio_entities=("EMAIL_ADDRESS", "PERSON"),
        )

    monkeypatch.setattr(traces_module, "_load_strict_value_redactor", fake_loader)

    result = import_trace_source(
        provider="jsonl",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
        redact="strict",
    )

    row = _read_jsonl(out)[0]
    assert row["input"] == "[redacted]"
    assert row["raw"]["input"] == "[redacted]"
    assert result.warnings == ["strict value redaction was applied"]
    assert result.manifest_path is not None
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["redaction"]["mode"] == "strict"
    assert manifest["redaction"]["requested_mode"] == "strict"
    assert manifest["redaction"]["ruleset_hash"] == traces_module._STRICT_RULESET_HASH
    assert manifest["redaction"]["strict"]["available"] is True
    assert manifest["redaction"]["strict"]["entities"] == ["EMAIL_ADDRESS", "PERSON"]
    assert manifest["redaction"]["strict"]["entity_source"] == traces_module._PRESIDIO_ENTITY_SOURCE
    assert manifest["redaction"]["strict"]["dependencies"]["presidio-analyzer"] == "test"
    assert "input" in manifest["redaction"]["strict"]["text_leaf_keys"]
    assert manifest["redaction"]["values_redacted"] is True


def test_import_strict_skips_identifiers_and_redacts_free_text_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_text(
        json.dumps(
            {
                "trace_id": "trace_Alice",
                "span_id": "span_Alice",
                "name": "Alice",
                "kind": "tool",
                "tool_name": "Alice",
                "status": "ok",
                "status_message": "Alice failed after emailing alice@example.com",
                "input": "Ask Alice at alice@example.com",
                "attributes": {
                    "custom": "Alice custom",
                    "input.value": "Email alice@example.com",
                    "kensa.step.state_summary": "Alice summary",
                    "openinference.tool.name": "Alice",
                    "service.name": "Alice service",
                },
                "events": [
                    {
                        "name": "Alice",
                        "attributes": {
                            "exception.message": "Alice exploded",
                            "service.name": "Alice service",
                        },
                    }
                ],
            }
        )
        + "\n"
    )
    out = tmp_path / "imports" / "strict-paths.jsonl"
    scanned: list[str] = []

    def fake_loader() -> traces_module._StrictValueRedactor:
        def redact_pii(value: str) -> tuple[str, bool]:
            scanned.append(value)
            redacted = value.replace("Alice", "[redacted]")
            redacted = redacted.replace("alice@example.com", "[redacted]")
            return redacted, redacted != value

        return traces_module._StrictValueRedactor(
            detect_secret=lambda value: False,
            redact_pii=redact_pii,
            dependencies={
                "detect-secrets": "test",
                "en-core-web-sm": "test",
                "presidio-analyzer": "test",
                "spacy": "test",
            },
            presidio_entities=("EMAIL_ADDRESS", "PERSON"),
        )

    monkeypatch.setattr(traces_module, "_load_strict_value_redactor", fake_loader)

    result = import_trace_source(
        provider="jsonl",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
        redact="strict",
    )

    row = _read_jsonl(out)[0]
    span = row["spans"][0]
    assert row["id"] == "trace_Alice"
    assert span["id"] == "span_Alice"
    assert span["name"] == "Alice"
    assert span["tool_name"] == "Alice"
    assert span["status"] == "ok"
    assert span["status_message"] == "[redacted] failed after emailing [redacted]"
    assert span["input"] == "Ask [redacted] at [redacted]"
    assert span["attributes"]["custom"] == "Alice custom"
    assert span["attributes"]["input.value"] == "Email [redacted]"
    assert span["attributes"]["kensa.step.state_summary"] == "[redacted] summary"
    assert span["attributes"]["openinference.tool.name"] == "Alice"
    assert span["attributes"]["service.name"] == "Alice service"
    assert span["events"][0]["name"] == "Alice"
    assert span["events"][0]["attributes"]["exception.message"] == "[redacted] exploded"
    assert span["events"][0]["attributes"]["service.name"] == "Alice service"
    assert "Alice failed after emailing alice@example.com" in scanned
    assert "Ask Alice at alice@example.com" in scanned
    assert "Email alice@example.com" in scanned
    assert "Alice summary" in scanned
    assert "Alice exploded" in scanned
    assert result.warnings == ["strict value redaction was applied"]


def test_import_strict_redacts_presidio_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_text(
        json.dumps({"id": "tr_1", "input": "Email Alice at alice@example.com"}) + "\n"
    )
    out = tmp_path / "imports" / "presidio.jsonl"

    def fake_loader() -> traces_module._StrictValueRedactor:
        def redact_pii(value: str) -> tuple[str, bool]:
            redacted = value.replace("Alice", "[redacted]")
            redacted = redacted.replace("alice@example.com", "[redacted]")
            return redacted, redacted != value

        return traces_module._StrictValueRedactor(
            detect_secret=lambda value: False,
            redact_pii=redact_pii,
            dependencies={
                "detect-secrets": "test",
                "en-core-web-sm": "test",
                "presidio-analyzer": "test",
                "spacy": "test",
            },
            presidio_entities=("CREDIT_CARD", "EMAIL_ADDRESS", "PERSON", "US_SSN"),
        )

    monkeypatch.setattr(traces_module, "_load_strict_value_redactor", fake_loader)

    result = import_trace_source(
        provider="jsonl",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
        redact="strict",
    )

    row = _read_jsonl(out)[0]
    assert row["input"] == "Email [redacted] at [redacted]"
    assert result.warnings == ["strict value redaction was applied"]
    assert result.manifest_path is not None
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["redaction"]["strict"]["entities"] == [
        "CREDIT_CARD",
        "EMAIL_ADDRESS",
        "PERSON",
        "US_SSN",
    ]
    assert manifest["redaction"]["values_redacted"] is True


def test_import_strict_redacts_with_real_presidio_and_spacy_model(tmp_path: Path) -> None:
    pytest.importorskip("detect_secrets")
    pytest.importorskip("en_core_web_sm")
    pytest.importorskip("presidio_analyzer")
    pytest.importorskip("spacy")
    source = tmp_path / "traces.jsonl"
    source.write_text(
        json.dumps({"id": "tr_1", "input": "Please email alice@example.com after the run."}) + "\n"
    )
    out = tmp_path / "imports" / "real-presidio.jsonl"

    result = import_trace_source(
        provider="jsonl",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
        redact="strict",
    )

    row = _read_jsonl(out)[0]
    assert "alice@example.com" not in row["input"]
    assert "[redacted]" in row["input"]
    assert "strict value redaction was applied" in result.warnings
    assert result.redaction["mode"] == "strict"
    assert "EMAIL_ADDRESS" in result.redaction["strict"]["entities"]


def test_import_strict_falls_back_to_key_redaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_text(json.dumps({"id": "tr_1", "api_key": "secret", "input": "Alice"}) + "\n")
    out = tmp_path / "imports" / "strict.jsonl"

    def missing_loader() -> traces_module._StrictValueRedactor:
        raise RuntimeError("Presidio analyzer unavailable: model missing")

    monkeypatch.setattr(traces_module, "_load_strict_value_redactor", missing_loader)

    result = import_trace_source(
        provider="jsonl",
        source=str(source),
        out=out,
        limit=1,
        max_payload_bytes=source.stat().st_size,
        redact="strict",
    )

    row = _read_jsonl(out)[0]
    assert row["raw"]["api_key"] == "[redacted]"
    assert row["input"] == "Alice"
    assert result.warnings == [
        (
            "strict redaction unavailable; fell back to key-only redaction: "
            "Presidio analyzer unavailable: model missing"
        ),
        "secret-like fields were redacted",
    ]
    assert result.manifest_path is not None
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["redaction"]["mode"] == "keys"
    assert manifest["redaction"]["requested_mode"] == "strict"
    assert manifest["redaction"]["strict"]["available"] is False
    assert manifest["redaction"]["strict"]["fallback_reason"] == (
        "Presidio analyzer unavailable: model missing"
    )


def test_strict_value_redactor_loader_scans_detect_secrets_and_presidio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSettings:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *args: object) -> None:
            return None

    class FakeProvider:
        def __init__(self, nlp_configuration: dict[str, object]) -> None:
            assert nlp_configuration["nlp_engine_name"] == "spacy"

        def create_engine(self) -> str:
            return "nlp-engine"

    class FakeAnalyzer:
        def __init__(self, nlp_engine: str, supported_languages: list[str]) -> None:
            assert nlp_engine == "nlp-engine"
            assert supported_languages == ["en"]

        def get_supported_entities(self, language: str) -> list[str]:
            assert language == "en"
            return ["PERSON", "EMAIL_ADDRESS", "US_SSN"]

        def analyze(self, text: str, language: str, entities: list[str]) -> list[SimpleNamespace]:
            assert language == "en"
            assert entities == ["EMAIL_ADDRESS", "PERSON", "US_SSN"]
            start = text.index("alice@example.com")
            end = start + len("alice@example.com")
            return [SimpleNamespace(start=start, end=end)]

    def scan_line(*args: object, **kwargs: object) -> list[str]:
        line = str(args[0] if len(args) == 1 else kwargs.get("line", ""))
        if len(args) == 1 and ("keyword-token" in line or "legacy-token" in line):
            raise TypeError
        if kwargs and "legacy-token" in line:
            raise TypeError
        if len(args) > 1:
            line = str(args[1])
        return ["secret"] if "token" in line else []

    def fake_import_module(name: str) -> object:
        modules = {
            "detect_secrets.core.scan": SimpleNamespace(scan_line=scan_line),
            "detect_secrets.settings": SimpleNamespace(default_settings=FakeSettings),
            "presidio_analyzer": SimpleNamespace(AnalyzerEngine=FakeAnalyzer),
            "presidio_analyzer.nlp_engine": SimpleNamespace(NlpEngineProvider=FakeProvider),
        }
        return modules[name]

    def fake_version(package: str) -> str:
        if package == "spacy":
            raise traces_module.importlib.metadata.PackageNotFoundError
        return f"{package}-version"

    monkeypatch.setattr(traces_module.importlib, "import_module", fake_import_module)
    monkeypatch.setattr(traces_module.importlib.metadata, "version", fake_version)

    redactor = traces_module._load_strict_value_redactor()

    assert redactor.presidio_entities == ("EMAIL_ADDRESS", "PERSON", "US_SSN")
    assert redactor.dependencies == {
        "detect-secrets": "detect-secrets-version",
        "en-core-web-sm": "en-core-web-sm-version",
        "presidio-analyzer": "presidio-analyzer-version",
        "spacy": "unknown",
    }
    assert redactor.detect_secret(" ") is False
    assert redactor.detect_secret("token") is True
    assert redactor.detect_secret("keyword-token") is True
    assert redactor.detect_secret("legacy-token") is True
    assert redactor.redact_pii("Email alice@example.com")[0] == "Email [redacted]"


def test_strict_value_redactor_loader_reports_missing_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_import(_name: str) -> object:
        raise ImportError("missing")

    monkeypatch.setattr(traces_module.importlib, "import_module", missing_import)

    with pytest.raises(RuntimeError, match="strict redaction dependencies unavailable"):
        traces_module._load_strict_value_redactor()


def test_strict_value_redactor_loader_reports_presidio_setup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenProvider:
        def __init__(self, nlp_configuration: dict[str, object]) -> None:
            assert nlp_configuration

        def create_engine(self) -> str:
            raise ValueError("model missing")

    def fake_import_module(name: str) -> object:
        modules = {
            "detect_secrets.core.scan": SimpleNamespace(scan_line=lambda *args, **kwargs: []),
            "detect_secrets.settings": SimpleNamespace(default_settings=lambda: None),
            "presidio_analyzer": SimpleNamespace(AnalyzerEngine=object),
            "presidio_analyzer.nlp_engine": SimpleNamespace(NlpEngineProvider=BrokenProvider),
        }
        return modules[name]

    monkeypatch.setattr(traces_module.importlib, "import_module", fake_import_module)

    with pytest.raises(RuntimeError, match="Presidio analyzer unavailable"):
        traces_module._load_strict_value_redactor()


def test_strict_value_redactor_loader_rejects_empty_entity_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSettings:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *args: object) -> None:
            return None

    class FakeProvider:
        def __init__(self, nlp_configuration: dict[str, object]) -> None:
            assert nlp_configuration

        def create_engine(self) -> str:
            return "nlp-engine"

    class EmptyAnalyzer:
        def __init__(self, nlp_engine: str, supported_languages: list[str]) -> None:
            assert nlp_engine
            assert supported_languages

        def get_supported_entities(self, language: str) -> list[str]:
            assert language == "en"
            return []

    def fake_import_module(name: str) -> object:
        modules = {
            "detect_secrets.core.scan": SimpleNamespace(scan_line=lambda *args, **kwargs: []),
            "detect_secrets.settings": SimpleNamespace(default_settings=FakeSettings),
            "presidio_analyzer": SimpleNamespace(AnalyzerEngine=EmptyAnalyzer),
            "presidio_analyzer.nlp_engine": SimpleNamespace(NlpEngineProvider=FakeProvider),
        }
        return modules[name]

    monkeypatch.setattr(traces_module.importlib, "import_module", fake_import_module)

    with pytest.raises(RuntimeError, match="no supported English entities"):
        traces_module._load_strict_value_redactor()


@pytest.mark.parametrize("provider", ["json", "jsonl"])
def test_import_json_trace_records_accept_trace_id_without_id(
    provider: str,
    tmp_path: Path,
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


def test_import_otlp_records_groups_spans_into_trace_view(tmp_path: Path) -> None:
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
    assert span["attributes"]["authorization"] == "[redacted]"
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


def test_import_langfuse_records_preserve_trace_and_observation_fields(tmp_path: Path) -> None:
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
        project="support-agent",
        since="24h",
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
    assert manifest["project"] == "support-agent"
    assert manifest["since"] == "24h"
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


def test_import_langfuse_records_accepts_official_data_envelope(tmp_path: Path) -> None:
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
    with pytest.raises(ValueError, match="Re-import traces with kensa import"):
        load_trace_views(old_artifact)


def test_import_trace_source_validates_bounds_provider_and_mechanical_ids(tmp_path: Path) -> None:
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
    with pytest.raises(ValueError, match="bounded trace export"):
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
    with pytest.raises(ValueError, match="--redact"):
        traces_module._validate_redaction_mode("bad")
    assert traces_module._should_scan_strict_value(()) is False

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

    assert traces_module._sensitive_warnings([{"nested": [{"api_key": "secret"}]}]) == [
        "secret-like fields were redacted"
    ]
    assert traces_module._contains_secret_key(["plain"]) is False
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
        traces_module._write_text_atomic(output, "data")
    assert not list(output.parent.glob(f".{output.name}.*"))

    with pytest.raises(ValueError, match="unsupported trace import provider"):
        traces_module._import_trace_views(
            provider="missing",
            source_path=source,
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
    source.write_text("\n{\n")
    with pytest.raises(ValueError, match="Re-import traces with kensa import"):
        load_trace_views(source)

    source.write_text("[]\n")
    with pytest.raises(ValueError, match="Re-import traces with kensa import"):
        load_trace_views(source)

    trace = _minimal_trace_view()
    invalid = dict(trace)
    invalid["extra"] = True
    source.write_text(json.dumps(invalid) + "\n")
    with pytest.raises(ValueError, match="Re-import traces with kensa import"):
        load_trace_views(source)

    invalid = dict(trace)
    invalid["source"] = {}
    source.write_text(json.dumps(invalid) + "\n")
    with pytest.raises(ValueError, match="Re-import traces with kensa import"):
        load_trace_views(source)

    invalid = dict(trace)
    invalid["spans"] = {}
    source.write_text(json.dumps(invalid) + "\n")
    with pytest.raises(ValueError, match="Re-import traces with kensa import"):
        load_trace_views(source)

    invalid = dict(trace)
    invalid["spans"] = [{}]
    source.write_text(json.dumps(invalid) + "\n")
    with pytest.raises(ValueError, match="Re-import traces with kensa import"):
        load_trace_views(source)

    invalid = dict(trace)
    invalid["id"] = ""
    source.write_text(json.dumps(invalid) + "\n")
    with pytest.raises(ValueError, match="missing id"):
        load_trace_views(source)
