use std::str::FromStr;

use kensa_core::protocol::{
    CaseId, CheckResultId, EvalRunId, InvocationId, NonBlankString, SpanId, Timestamp, TraceId,
};

const UUID_V7: &str = "01890f47-6c20-7a81-b1c8-9e6f12d6406e";

#[test]
fn kensa_identifiers_accept_canonical_uuid_v7_values() {
    for value in [
        format!("run_{UUID_V7}"),
        format!("inv_{UUID_V7}"),
        format!("chk_{UUID_V7}"),
    ] {
        assert!(!value.parse::<EvalRunId>().is_ok() || value.starts_with("run_"));
        assert!(!value.parse::<InvocationId>().is_ok() || value.starts_with("inv_"));
        assert!(!value.parse::<CheckResultId>().is_ok() || value.starts_with("chk_"));
    }

    assert_eq!(
        EvalRunId::from_str(&format!("run_{UUID_V7}"))
            .expect("valid run identifier")
            .as_str(),
        format!("run_{UUID_V7}")
    );
    assert_eq!(
        InvocationId::from_str(&format!("inv_{UUID_V7}"))
            .expect("valid invocation identifier")
            .as_str(),
        format!("inv_{UUID_V7}")
    );
    assert_eq!(
        CheckResultId::from_str(&format!("chk_{UUID_V7}"))
            .expect("valid check identifier")
            .as_str(),
        format!("chk_{UUID_V7}")
    );
}

#[test]
fn kensa_identifiers_reject_noncanonical_or_non_v7_values() {
    let invalid_suffixes = [
        "550e8400-e29b-41d4-a716-446655440000",
        "01890F47-6C20-7A81-B1C8-9E6F12D6406E",
        "01890f476c207a81b1c89e6f12d6406e",
        "01890f47-6c20-7a81-71c8-9e6f12d6406e",
        "not-a-uuid",
    ];

    for suffix in invalid_suffixes {
        assert!(format!("run_{suffix}").parse::<EvalRunId>().is_err());
        assert!(format!("inv_{suffix}").parse::<InvocationId>().is_err());
        assert!(format!("chk_{suffix}").parse::<CheckResultId>().is_err());
    }

    assert!(format!("inv_{UUID_V7}").parse::<EvalRunId>().is_err());
    assert!(format!("run_{UUID_V7}").parse::<InvocationId>().is_err());
    assert!(format!("run_{UUID_V7}").parse::<CheckResultId>().is_err());
}

#[test]
fn case_and_nonblank_strings_reject_blank_values() {
    for value in ["", " ", "\n\t"] {
        assert!(value.parse::<CaseId>().is_err());
        assert!(value.parse::<NonBlankString>().is_err());
    }

    assert_eq!("case 1".parse::<CaseId>().unwrap().as_str(), "case 1");
    assert_eq!("value".parse::<NonBlankString>().unwrap().as_str(), "value");
}

#[test]
fn trace_and_span_identifiers_enforce_w3c_wire_form() {
    let trace = "4bf92f3577b34da6a3ce929d0e0e4736";
    let span = "00f067aa0ba902b7";
    assert_eq!(trace.parse::<TraceId>().unwrap().as_str(), trace);
    assert_eq!(span.parse::<SpanId>().unwrap().as_str(), span);

    for value in [
        "00000000000000000000000000000000",
        "4BF92F3577B34DA6A3CE929D0E0E4736",
        "4bf92f3577b34da6a3ce929d0e0e473",
        "4bf92f3577b34da6a3ce929d0e0e473g",
    ] {
        assert!(
            value.parse::<TraceId>().is_err(),
            "accepted trace ID {value}"
        );
    }

    for value in [
        "0000000000000000",
        "00F067AA0BA902B7",
        "00f067aa0ba902b",
        "00f067aa0ba902bg",
    ] {
        assert!(value.parse::<SpanId>().is_err(), "accepted span ID {value}");
    }
}

#[test]
fn timestamps_accept_only_real_canonical_millisecond_utc_values() {
    for value in [
        "0000-01-01T00:00:00.000Z",
        "2026-08-06T00:00:00.000Z",
        "2026-04-30T12:30:45.123Z",
        "2024-02-29T23:59:59.999Z",
        "9999-12-31T23:59:59.999Z",
    ] {
        assert_eq!(value.parse::<Timestamp>().unwrap().as_str(), value);
    }

    for value in [
        "2026-08-06T00:00:00.000+00:00",
        "2026-08-06T00:00:00Z",
        "2026-08-06T00:00:00.0Z",
        "2026-08-06T00:00:00.00Z",
        "2026-08-06T00:00:00.0000Z",
        "2026-08-06t00:00:00.000Z",
        "2026-08-06T00:00:00.000z",
        "2026-08-06T00:00:60.000Z",
        "2023-02-29T00:00:00.000Z",
        "2026-13-01T00:00:00.000Z",
        "2026-01-32T00:00:00.000Z",
        "2026-01-01T24:00:00.000Z",
        "2026-01-01T00:60:00.000Z",
        " 2026-08-06T00:00:00.000Z",
    ] {
        assert!(
            value.parse::<Timestamp>().is_err(),
            "accepted timestamp {value}"
        );
    }
}
