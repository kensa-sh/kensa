use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use kensa_core::protocol::{RejectionBoundary, canonical_json, generated_schemas, parse_document};
use serde::Deserialize;
use serde_json::Value;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    schema_version: String,
    fixtures: Vec<Fixture>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Fixture {
    file: String,
    valid: bool,
    document_kind: String,
    expected_rejection_boundary: Option<RejectionBoundary>,
}

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn read_json(path: &Path) -> Value {
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

fn manifest() -> Manifest {
    serde_json::from_value(read_json(
        &root().join("fixtures/conformance/v1/manifest.json"),
    ))
    .unwrap()
}

fn schema_filename(kind: &str) -> &str {
    match kind {
        "eval_run" => "eval-run.schema.json",
        "invocation" => "invocation.schema.json",
        "span" => "span.schema.json",
        "check_result" => "check-result.schema.json",
        value => panic!("unknown fixture document kind: {value}"),
    }
}

#[test]
fn generated_schemas_match_committed_canonical_bytes() {
    let schemas = generated_schemas().unwrap();
    assert_eq!(
        schemas.keys().map(String::as_str).collect::<BTreeSet<_>>(),
        BTreeSet::from([
            "check-result.schema.json",
            "eval-run.schema.json",
            "invocation.schema.json",
            "protocol-document.schema.json",
            "span.schema.json",
        ])
    );

    for (filename, schema) in schemas {
        let expected = fs::read(root().join("schemas/v1").join(filename)).unwrap();
        assert_eq!(canonical_json(&schema).unwrap(), expected);
    }
}

#[test]
fn valid_fixtures_pass_both_schemas_and_round_trip_canonically() {
    let manifest = manifest();
    assert_eq!(manifest.schema_version, "kensa.conformance.v1");
    let schemas = generated_schemas().unwrap();
    let union_validator = jsonschema::draft202012::options()
        .build(&schemas["protocol-document.schema.json"])
        .unwrap();
    let mut kinds = BTreeSet::new();

    for fixture in manifest.fixtures.iter().filter(|fixture| fixture.valid) {
        assert!(fixture.expected_rejection_boundary.is_none());
        let bytes = fs::read(root().join("fixtures/conformance/v1").join(&fixture.file)).unwrap();
        let value: Value = serde_json::from_slice(&bytes).unwrap();
        let kind_validator = jsonschema::draft202012::options()
            .build(&schemas[schema_filename(&fixture.document_kind)])
            .unwrap();
        assert!(
            kind_validator.is_valid(&value),
            "{} failed kind schema",
            fixture.file
        );
        assert!(
            union_validator.is_valid(&value),
            "{} failed union schema",
            fixture.file
        );

        let document = parse_document(&bytes).unwrap();
        assert_eq!(document.document_kind(), fixture.document_kind);
        assert_eq!(
            canonical_json(&document).unwrap(),
            bytes,
            "{} is not canonical",
            fixture.file
        );
        kinds.insert(fixture.document_kind.as_str());
    }

    assert_eq!(
        kinds,
        BTreeSet::from(["check_result", "eval_run", "invocation", "span"])
    );
}

#[test]
fn negative_fixtures_fail_at_the_declared_stable_boundary() {
    let manifest = manifest();
    let schemas = generated_schemas().unwrap();

    for fixture in manifest.fixtures.iter().filter(|fixture| !fixture.valid) {
        let boundary = fixture
            .expected_rejection_boundary
            .expect("negative fixture must declare a rejection boundary");
        let bytes = fs::read(root().join("fixtures/conformance/v1").join(&fixture.file)).unwrap();
        let value: Value = serde_json::from_slice(&bytes).unwrap();

        match boundary {
            RejectionBoundary::Schema => {
                let validator = jsonschema::draft202012::options()
                    .build(&schemas[schema_filename(&fixture.document_kind)])
                    .unwrap();
                assert!(
                    !validator.is_valid(&value),
                    "{} passed its schema",
                    fixture.file
                );
            }
            RejectionBoundary::Rust => {
                let error = parse_document(&bytes).unwrap_err();
                assert!(!error.message().is_empty(), "{}", fixture.file);
                assert_eq!(
                    error.boundary(),
                    RejectionBoundary::Rust,
                    "{}",
                    fixture.file
                );
            }
        }
    }
}

#[test]
fn fixture_manifest_has_required_coverage_and_no_unlisted_json_files() {
    let manifest = manifest();
    let listed = manifest
        .fixtures
        .iter()
        .map(|fixture| fixture.file.as_str())
        .collect::<BTreeSet<_>>();
    assert!(
        manifest
            .fixtures
            .iter()
            .any(|fixture| fixture.file.contains("minimal"))
    );
    assert!(
        manifest
            .fixtures
            .iter()
            .any(|fixture| fixture.file.contains("complete"))
    );

    let values = manifest
        .fixtures
        .iter()
        .filter(|fixture| fixture.valid)
        .map(|fixture| read_json(&root().join("fixtures/conformance/v1").join(&fixture.file)))
        .collect::<Vec<_>>();
    let categories = values
        .iter()
        .filter_map(|value| value.pointer("/failure/category").and_then(Value::as_str))
        .collect::<BTreeSet<_>>();
    assert_eq!(
        categories,
        BTreeSet::from([
            "agent",
            "configuration",
            "harness",
            "infrastructure",
            "judge",
            "simulator",
            "unknown",
        ])
    );
    let evidence_statuses = values
        .iter()
        .filter_map(|value| {
            value
                .pointer("/evidence_completeness/status")
                .and_then(Value::as_str)
        })
        .collect::<BTreeSet<_>>();
    assert_eq!(
        evidence_statuses,
        BTreeSet::from(["complete", "partial", "pending", "unavailable"])
    );

    let fixture_root = root().join("fixtures/conformance/v1");
    let mut discovered = BTreeMap::new();
    for directory in ["valid", "invalid"] {
        for entry in fs::read_dir(fixture_root.join(directory)).unwrap() {
            let path = entry.unwrap().path();
            if path
                .extension()
                .is_some_and(|extension| extension == "json")
            {
                let relative = path.strip_prefix(&fixture_root).unwrap();
                discovered.insert(relative.to_string_lossy().replace('\\', "/"), ());
            }
        }
    }
    assert_eq!(listed, discovered.keys().map(String::as_str).collect());
}
