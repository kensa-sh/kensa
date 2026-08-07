use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use kensa_core::protocol::{
    CheckResultDocumentKind, CheckStatus, EffectPolicy, EvalRunDocumentKind, EvalRunStatus,
    EvidenceStatus, FailureCategory, InvocationDocumentKind, InvocationStatus, JsonObject,
    SchemaVersion, SpanDocumentKind, SpanStatus, generated_schemas, parse_document,
};
use serde_json::{Value, json};

fn string_set(value: &Value) -> BTreeSet<&str> {
    value
        .as_array()
        .unwrap()
        .iter()
        .map(|item| item.as_str().unwrap())
        .collect()
}

fn assert_closed_object(schema: &Value, pointer: &str, expected_fields: &[&str]) {
    let object = schema.pointer(pointer).unwrap();
    let properties = object["properties"].as_object().unwrap();
    let expected = expected_fields.iter().copied().collect::<BTreeSet<_>>();

    assert_eq!(
        properties
            .keys()
            .map(String::as_str)
            .collect::<BTreeSet<_>>(),
        expected
    );
    assert_eq!(string_set(&object["required"]), expected);
    assert_eq!(object["additionalProperties"], json!(false));
}

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn fixture(path: &str) -> Value {
    serde_json::from_slice(&fs::read(root().join("fixtures/conformance/v1").join(path)).unwrap())
        .unwrap()
}

fn assert_required_fields_rejected(value: &Value, pointer: &str, fields: &[&str]) {
    for field in fields {
        let mut missing = value.clone();
        missing
            .pointer_mut(pointer)
            .unwrap()
            .as_object_mut()
            .unwrap()
            .remove(*field)
            .unwrap();
        assert!(
            parse_document(&serde_json::to_vec(&missing).unwrap()).is_err(),
            "accepted missing field `{field}` at `{pointer}`"
        );
    }
}

#[test]
fn protocol_surface_exposes_wire_discriminants_and_object_type() {
    assert_eq!(
        serde_json::to_value(SchemaVersion::V1).unwrap(),
        json!("kensa.protocol.v1")
    );
    assert_eq!(
        serde_json::to_value(EvalRunDocumentKind::EvalRun).unwrap(),
        json!("eval_run")
    );
    assert_eq!(
        serde_json::to_value(InvocationDocumentKind::Invocation).unwrap(),
        json!("invocation")
    );
    assert_eq!(
        serde_json::to_value(SpanDocumentKind::Span).unwrap(),
        json!("span")
    );
    assert_eq!(
        serde_json::to_value(CheckResultDocumentKind::CheckResult).unwrap(),
        json!("check_result")
    );

    let object: JsonObject = BTreeMap::new();
    assert!(object.is_empty());
}

#[test]
fn generated_schemas_have_the_exact_wire_fields() {
    let schemas = generated_schemas().unwrap();

    assert_closed_object(
        &schemas["eval-run.schema.json"],
        "",
        &[
            "schema_version",
            "document_kind",
            "id",
            "status",
            "created_at",
            "started_at",
            "ended_at",
            "duration_ms",
            "attributes",
            "failure",
        ],
    );
    assert_closed_object(
        &schemas["invocation.schema.json"],
        "",
        &[
            "schema_version",
            "document_kind",
            "id",
            "run_id",
            "case",
            "attempt",
            "status",
            "started_at",
            "ended_at",
            "duration_ms",
            "output_recorded",
            "output",
            "provenance",
            "evidence_completeness",
            "attributes",
            "failure",
        ],
    );
    assert_closed_object(
        &schemas["span.schema.json"],
        "",
        &[
            "schema_version",
            "document_kind",
            "invocation_id",
            "trace_id",
            "span_id",
            "parent_span_id",
            "name",
            "span_kind",
            "status",
            "status_message",
            "started_at",
            "ended_at",
            "duration_ms",
            "input_recorded",
            "input",
            "output_recorded",
            "output",
            "attributes",
        ],
    );
    assert_closed_object(
        &schemas["check-result.schema.json"],
        "",
        &[
            "schema_version",
            "document_kind",
            "id",
            "invocation_id",
            "name",
            "status",
            "started_at",
            "ended_at",
            "duration_ms",
            "evidence",
            "failure",
        ],
    );

    let invocation_schema = &schemas["invocation.schema.json"];
    assert_closed_object(
        invocation_schema,
        "/$defs/CaseSnapshot",
        &["id", "input", "metadata"],
    );
    assert_closed_object(
        invocation_schema,
        "/$defs/Failure",
        &["category", "kind", "message", "evidence"],
    );
    assert_closed_object(
        invocation_schema,
        "/$defs/ExecutionProvenance",
        &[
            "producer",
            "producer_version",
            "adapter",
            "adapter_version",
            "runtime",
            "runtime_version",
            "revision",
            "environment",
            "effects",
        ],
    );
    assert_closed_object(
        invocation_schema,
        "/$defs/EvidenceCompleteness",
        &["status", "reason"],
    );
}

#[test]
fn rust_rejects_every_missing_top_level_and_embedded_field() {
    let schemas = generated_schemas().unwrap();
    for (filename, fixture_path) in [
        ("eval-run.schema.json", "valid/eval-run-complete.json"),
        ("invocation.schema.json", "valid/invocation-complete.json"),
        ("span.schema.json", "valid/span-complete.json"),
        (
            "check-result.schema.json",
            "valid/check-result-minimal.json",
        ),
    ] {
        let fields = string_set(&schemas[filename]["required"])
            .into_iter()
            .collect::<Vec<_>>();
        assert_required_fields_rejected(&fixture(fixture_path), "", &fields);
    }

    let invocation = fixture("valid/invocation-complete.json");
    let invocation_schema = &schemas["invocation.schema.json"];
    for (pointer, definition) in [
        ("/case", "CaseSnapshot"),
        ("/provenance", "ExecutionProvenance"),
        ("/evidence_completeness", "EvidenceCompleteness"),
    ] {
        let fields = string_set(&invocation_schema["$defs"][definition]["required"])
            .into_iter()
            .collect::<Vec<_>>();
        assert_required_fields_rejected(&invocation, pointer, &fields);
    }

    let eval_run = fixture("valid/eval-run-failure-configuration.json");
    let fields = string_set(&schemas["eval-run.schema.json"]["$defs"]["Failure"]["required"])
        .into_iter()
        .collect::<Vec<_>>();
    assert_required_fields_rejected(&eval_run, "/failure", &fields);
}

#[test]
fn generated_schemas_have_the_exact_closed_values_and_union_members() {
    let schemas = generated_schemas().unwrap();
    let union = &schemas["protocol-document.schema.json"];

    for (name, expected) in [
        ("SchemaVersion", &["kensa.protocol.v1"][..]),
        ("EvalRunDocumentKind", &["eval_run"][..]),
        ("InvocationDocumentKind", &["invocation"][..]),
        ("SpanDocumentKind", &["span"][..]),
        ("CheckResultDocumentKind", &["check_result"][..]),
        (
            "EvalRunStatus",
            &[
                "pending",
                "running",
                "pass",
                "fail",
                "error",
                "cancelled",
                "interrupted",
            ][..],
        ),
        (
            "InvocationStatus",
            &[
                "pending",
                "running",
                "pass",
                "fail",
                "error",
                "cancelled",
                "skipped",
                "interrupted",
            ][..],
        ),
        ("CheckStatus", &["pass", "fail", "error", "skipped"][..]),
        ("SpanStatus", &["unset", "ok", "error"][..]),
        (
            "FailureCategory",
            &[
                "agent",
                "simulator",
                "judge",
                "configuration",
                "infrastructure",
                "harness",
                "unknown",
            ][..],
        ),
        (
            "EffectPolicy",
            &["none", "captured", "sandboxed", "live"][..],
        ),
        (
            "EvidenceStatus",
            &["complete", "pending", "partial", "unavailable"][..],
        ),
    ] {
        assert_eq!(
            string_set(&union["$defs"][name]["enum"]),
            expected.iter().copied().collect(),
            "{name}"
        );
    }

    assert_eq!(
        union["oneOf"],
        json!([
            {"$ref": "#/$defs/EvalRun"},
            {"$ref": "#/$defs/Invocation"},
            {"$ref": "#/$defs/Span"},
            {"$ref": "#/$defs/CheckResult"}
        ])
    );

    let _closed_enums = (
        EvalRunStatus::Pending,
        InvocationStatus::Pending,
        CheckStatus::Pass,
        SpanStatus::Unset,
        FailureCategory::Agent,
        EffectPolicy::None,
        EvidenceStatus::Complete,
    );
}
