use kensa_core::protocol::{
    CheckResult, EvalRun, EvidenceCompleteness, EvidenceStatus, Failure, Invocation, Span,
    canonical_json,
};
use serde::Serialize;
use serde_json::{Value, json};

fn failure() -> Value {
    json!({
        "category": "agent",
        "kind": "timeout",
        "message": "Timed out",
        "evidence": {}
    })
}

fn eval_run(status: &str, failure: Value) -> Value {
    json!({
        "schema_version": "kensa.protocol.v1",
        "document_kind": "eval_run",
        "id": "run_01890f47-6c20-7a81-b1c8-9e6f12d6406e",
        "status": status,
        "created_at": "2026-08-06T00:00:00.000Z",
        "started_at": null,
        "ended_at": null,
        "duration_ms": null,
        "attributes": {},
        "failure": failure
    })
}

fn invocation() -> Value {
    json!({
        "schema_version": "kensa.protocol.v1",
        "document_kind": "invocation",
        "id": "inv_01890f47-6c20-7a81-b1c8-9e6f12d6406e",
        "run_id": "run_01890f47-6c20-7a81-b1c8-9e6f12d6406e",
        "case": {"id": "case-1", "input": null, "metadata": {}},
        "attempt": 1,
        "status": "pass",
        "started_at": null,
        "ended_at": null,
        "duration_ms": null,
        "output_recorded": true,
        "output": null,
        "provenance": {
            "producer": "kensa",
            "producer_version": "1.0.0",
            "adapter": null,
            "adapter_version": null,
            "runtime": "python",
            "runtime_version": "3.12",
            "revision": null,
            "environment": null,
            "effects": "none"
        },
        "evidence_completeness": {"status": "complete", "reason": null},
        "attributes": {},
        "failure": null
    })
}

fn span() -> Value {
    json!({
        "schema_version": "kensa.protocol.v1",
        "document_kind": "span",
        "invocation_id": "inv_01890f47-6c20-7a81-b1c8-9e6f12d6406e",
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "span_id": "00f067aa0ba902b7",
        "parent_span_id": null,
        "name": "invoke",
        "span_kind": "internal",
        "status": "ok",
        "status_message": null,
        "started_at": null,
        "ended_at": null,
        "duration_ms": null,
        "input_recorded": false,
        "input": null,
        "output_recorded": true,
        "output": null,
        "attributes": {}
    })
}

fn check_result(status: &str, failure: Value) -> Value {
    json!({
        "schema_version": "kensa.protocol.v1",
        "document_kind": "check_result",
        "id": "chk_01890f47-6c20-7a81-b1c8-9e6f12d6406e",
        "invocation_id": "inv_01890f47-6c20-7a81-b1c8-9e6f12d6406e",
        "name": "quality",
        "status": status,
        "started_at": null,
        "ended_at": null,
        "duration_ms": null,
        "evidence": {},
        "failure": failure
    })
}

fn assert_not_serializable<T: Serialize>(value: &T) {
    assert!(serde_json::to_value(value).is_err());
    assert!(canonical_json(value).is_err());
}

#[test]
fn contradictory_rust_values_cannot_produce_protocol_json() {
    let evidence_completeness = EvidenceCompleteness {
        status: EvidenceStatus::Complete,
        reason: Some("unexpected".parse().unwrap()),
    };
    assert_not_serializable(&evidence_completeness);

    let failure = serde_json::from_value::<Failure>(failure()).unwrap();

    let mut eval_run = serde_json::from_value::<EvalRun>(eval_run("pass", Value::Null)).unwrap();
    eval_run.failure = Some(failure.clone());
    assert_not_serializable(&eval_run);

    let invocation = serde_json::from_value::<Invocation>(invocation()).unwrap();
    let mut invalid_failure = invocation.clone();
    invalid_failure.failure = Some(failure.clone());
    assert_not_serializable(&invalid_failure);

    let mut invalid_output = invocation.clone();
    invalid_output.output_recorded = false;
    invalid_output.output = Some(json!({"value": 1}));
    assert_not_serializable(&invalid_output);

    let mut invalid_completeness = invocation;
    invalid_completeness.evidence_completeness.reason = Some("unexpected".parse().unwrap());
    assert_not_serializable(&invalid_completeness);

    let span = serde_json::from_value::<Span>(span()).unwrap();
    let mut invalid_message = span.clone();
    invalid_message.status_message = Some("unexpected".parse().unwrap());
    assert_not_serializable(&invalid_message);

    let mut invalid_input = span.clone();
    invalid_input.input = Some(json!({"value": 1}));
    assert_not_serializable(&invalid_input);

    let mut invalid_output = span;
    invalid_output.output_recorded = false;
    invalid_output.output = Some(json!({"value": 1}));
    assert_not_serializable(&invalid_output);

    let mut check_result =
        serde_json::from_value::<CheckResult>(check_result("pass", Value::Null)).unwrap();
    check_result.failure = Some(failure);
    assert_not_serializable(&check_result);
}

#[test]
fn status_and_failure_ownership_is_enforced() {
    for status in ["pending", "running", "pass"] {
        assert!(serde_json::from_value::<EvalRun>(eval_run(status, Value::Null)).is_ok());
        assert!(serde_json::from_value::<EvalRun>(eval_run(status, failure())).is_err());
    }
    for status in ["fail", "error", "cancelled", "interrupted"] {
        assert!(serde_json::from_value::<EvalRun>(eval_run(status, failure())).is_ok());
        assert!(serde_json::from_value::<EvalRun>(eval_run(status, Value::Null)).is_err());
    }

    assert!(serde_json::from_value::<CheckResult>(check_result("pass", Value::Null)).is_ok());
    assert!(serde_json::from_value::<CheckResult>(check_result("pass", failure())).is_err());
    for status in ["fail", "error", "skipped"] {
        assert!(serde_json::from_value::<CheckResult>(check_result(status, failure())).is_ok());
        assert!(serde_json::from_value::<CheckResult>(check_result(status, Value::Null)).is_err());
    }

    let mut value = invocation();
    for status in ["pending", "running", "pass"] {
        value["status"] = json!(status);
        value["failure"] = Value::Null;
        assert!(serde_json::from_value::<Invocation>(value.clone()).is_ok());
        value["failure"] = failure();
        assert!(serde_json::from_value::<Invocation>(value.clone()).is_err());
    }
    for status in ["fail", "error", "cancelled", "skipped", "interrupted"] {
        value["status"] = json!(status);
        value["failure"] = failure();
        assert!(serde_json::from_value::<Invocation>(value.clone()).is_ok());
        value["failure"] = Value::Null;
        assert!(serde_json::from_value::<Invocation>(value.clone()).is_err());
    }
}

#[test]
fn evidence_completeness_requires_an_unambiguous_reason() {
    assert!(
        serde_json::from_value::<EvidenceCompleteness>(
            json!({"status": "complete", "reason": null})
        )
        .is_ok()
    );
    assert!(
        serde_json::from_value::<EvidenceCompleteness>(
            json!({"status": "complete", "reason": "unexpected"})
        )
        .is_err()
    );

    for status in ["pending", "partial", "unavailable"] {
        assert!(
            serde_json::from_value::<EvidenceCompleteness>(
                json!({"status": status, "reason": "evidence missing"})
            )
            .is_ok()
        );
        assert!(
            serde_json::from_value::<EvidenceCompleteness>(
                json!({"status": status, "reason": null})
            )
            .is_err()
        );
    }
}

#[test]
fn presence_flags_distinguish_missing_evidence_from_recorded_null() {
    let mut invocation_value = invocation();
    invocation_value["output_recorded"] = json!(false);
    invocation_value["output"] = json!({"value": 1});
    assert!(serde_json::from_value::<Invocation>(invocation_value).is_err());

    let mut span_value = span();
    span_value["input_recorded"] = json!(false);
    span_value["input"] = json!(null);
    assert!(serde_json::from_value::<Span>(span_value.clone()).is_ok());
    span_value["input"] = json!("present");
    assert!(serde_json::from_value::<Span>(span_value).is_err());

    let mut span_value = span();
    span_value["output_recorded"] = json!(false);
    span_value["output"] = json!("present");
    assert!(serde_json::from_value::<Span>(span_value).is_err());
}

#[test]
fn span_error_status_requires_only_error_messages() {
    let mut value = span();
    value["status"] = json!("error");
    value["status_message"] = json!("request failed");
    assert!(serde_json::from_value::<Span>(value.clone()).is_ok());
    value["status_message"] = Value::Null;
    assert!(serde_json::from_value::<Span>(value).is_err());

    for status in ["unset", "ok"] {
        let mut value = span();
        value["status"] = json!(status);
        value["status_message"] = json!("not allowed");
        assert!(serde_json::from_value::<Span>(value).is_err());
    }
}

#[test]
fn unknown_fields_missing_fields_versions_and_discriminants_fail_closed() {
    let mut value = eval_run("pass", Value::Null);
    value["extra"] = json!(true);
    assert!(serde_json::from_value::<EvalRun>(value).is_err());

    let mut value = invocation();
    value["case"]["extra"] = json!(true);
    assert!(serde_json::from_value::<Invocation>(value).is_err());

    let mut value = eval_run("pass", Value::Null);
    value.as_object_mut().unwrap().remove("failure");
    assert!(serde_json::from_value::<EvalRun>(value).is_err());

    let mut value = eval_run("pass", Value::Null);
    value["schema_version"] = json!("kensa.protocol.v2");
    assert!(serde_json::from_value::<EvalRun>(value).is_err());

    let mut value = eval_run("pass", Value::Null);
    value["document_kind"] = json!("invocation");
    assert!(serde_json::from_value::<EvalRun>(value).is_err());
}
