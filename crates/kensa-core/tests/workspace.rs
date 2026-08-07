use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::Value;

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[test]
fn workspace_contains_exactly_the_three_planned_crates() {
    let output = Command::new(env!("CARGO"))
        .args(["metadata", "--no-deps", "--format-version", "1"])
        .current_dir(root())
        .output()
        .unwrap();
    assert!(output.status.success());
    let metadata: Value = serde_json::from_slice(&output.stdout).unwrap();
    let packages = metadata["packages"]
        .as_array()
        .unwrap()
        .iter()
        .map(|package| package["name"].as_str().unwrap())
        .collect::<BTreeSet<_>>();
    assert_eq!(
        packages,
        BTreeSet::from(["kensa-cli", "kensa-core", "kensa-server"])
    );
}

#[test]
fn planned_directories_exist_without_moving_python() {
    for directory in [
        "crates/kensa-core",
        "crates/kensa-server",
        "crates/kensa-cli",
        "fixtures/conformance/v1",
        "schemas/v1",
        "sdks/python",
        "sdks/typescript/packages/sdk",
        "sdks/typescript/packages/vitest",
        "web",
        "src/kensa",
        "tests",
    ] {
        assert!(root().join(directory).is_dir(), "missing {directory}");
    }
    for file in [
        "pyproject.toml",
        "uv.lock",
        "sdks/typescript/pnpm-workspace.yaml",
    ] {
        assert!(root().join(file).is_file(), "missing {file}");
    }
}

#[test]
fn future_runtime_crates_are_empty_placeholders() {
    for crate_name in ["kensa-server", "kensa-cli"] {
        let source =
            fs::read_to_string(root().join("crates").join(crate_name).join("src/lib.rs")).unwrap();
        assert!(
            source.trim().is_empty(),
            "{crate_name} contains runtime behavior"
        );
    }
}

#[test]
fn automation_covers_the_rust_contract_and_preserves_python_checks() {
    let ci = fs::read_to_string(root().join(".github/workflows/ci.yml")).unwrap();
    for command in [
        "cargo fmt --all --check",
        "cargo clippy --workspace --all-targets --all-features -- -D warnings",
        "cargo test -p kensa-core --test conformance generated_schemas_match_committed_canonical_bytes",
        "cargo test --workspace",
        "cargo llvm-cov --workspace --all-features --fail-under-lines 100",
        "python -m coverage run -m pytest -q -m \"not live\"",
        "python -m coverage report",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run ty check",
        "uv build",
    ] {
        assert!(ci.contains(command), "CI is missing `{command}`");
    }

    let dependabot = fs::read_to_string(root().join(".github/dependabot.yml")).unwrap();
    assert_eq!(
        dependabot.matches("package-ecosystem: \"cargo\"").count(),
        1
    );
    assert!(dependabot.contains("package-ecosystem: \"cargo\"\n    directory: \"/\""));
}
