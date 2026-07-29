from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from kensa.artifacts import write_run_artifacts
from kensa.errors import TrialFailure
from kensa.results import (
    AggregateResult,
    CostLatencySummary,
    ReliabilityCohort,
    ReliabilityPoint,
    ResultModel,
    RunInterruption,
    RunResult,
    RunSummary,
    TrialResult,
    load_run_result,
)
from kensa.runtime import TrialMetadata


def _trial(
    *,
    nodeid: str = "test_eval.py::test_agent[case-a-trial1]",
    group_id: str = "test_eval.py::test_agent[case-a]",
    case_id: str = "case-a",
    trial_index: int = 1,
    configured_trials: int = 1,
    status: str = "pass",
    failure: TrialFailure | None = None,
) -> TrialMetadata:
    return TrialMetadata(
        nodeid=nodeid,
        group_id=group_id,
        case_id=case_id,
        trial_index=trial_index,
        configured_trials=configured_trials,
        status=status,
        case={"id": case_id, "input": ["hello"]},
        output={"answer": ["world"]},
        failure=failure,
        duration_ms=12.5,
        trace={
            "spans": [{"span_id": "span-1"}],
            "agent_runs": [{"run_id": "external-run"}],
            "cost_usd": 0.1,
            "cost_available": True,
            "llm_turns": 1,
        },
        judges=[{"passed": True, "evidence": ["grounded"]}],
        active_operation={"name": "respond", "kind": "llm"},
    )


def _write(
    tmp_path: Path,
    *,
    trials: list[TrialMetadata] | None = None,
    complete: bool = True,
    interruption: dict[str, Any] | None = None,
) -> Path:
    result_path = tmp_path / "result.json"
    write_run_artifacts(
        run_id="run-1",
        trials=[] if trials is None else trials,
        result_path=result_path,
        artifact_dir=tmp_path,
        complete=complete,
        interruption=interruption,
    )
    return result_path


def test_results_exports_public_contract() -> None:
    import kensa.results as results

    assert {
        "AggregateResult",
        "AggregateVerdict",
        "CostLatencySummary",
        "ReliabilityCohort",
        "ReliabilityPoint",
        "ResultModel",
        "RunInterruption",
        "RunResult",
        "RunSummary",
        "RunStatus",
        "TrialPhase",
        "TrialResult",
        "load_run_result",
    } == set(results.__all__)
    assert issubclass(RunResult, ResultModel)


@pytest.mark.parametrize(
    ("complete", "interruption"),
    [
        (True, None),
        (False, None),
        (
            False,
            {
                "kind": "timeout",
                "message": "trial timed out",
                "nodeid": "test_eval.py::test_agent[case-a-trial1]",
                "case_id": "case-a",
                "trial_index": 1,
                "phase": "call",
            },
        ),
    ],
)
def test_loader_returns_typed_completed_initial_and_interrupted_results(
    tmp_path: Path,
    complete: bool,
    interruption: dict[str, Any] | None,
) -> None:
    trials = [] if not complete and interruption is None else [_trial()]
    result_path = _write(
        tmp_path,
        trials=trials,
        complete=complete,
        interruption=interruption,
    )

    result = load_run_result(result_path)

    assert isinstance(result, RunResult)
    assert result.schema_version == "kensa.result.v1"
    assert result.complete is complete
    assert isinstance(result.trials, tuple)
    assert isinstance(result.aggregates, tuple)
    assert isinstance(result.summary, RunSummary)
    assert isinstance(result.summary.cost_latency, CostLatencySummary)
    assert all(isinstance(trial, TrialResult) for trial in result.trials)
    assert all(isinstance(aggregate, AggregateResult) for aggregate in result.aggregates)
    assert all(isinstance(point, ReliabilityPoint) for point in result.summary.pass_k_curve)
    assert all(isinstance(cohort, ReliabilityCohort) for cohort in result.summary.pass_k_cohorts)
    if interruption is None:
        assert result.interruption is None
    else:
        assert isinstance(result.interruption, RunInterruption)
        assert result.interruption.kind == "timeout"


def test_loader_preserves_complete_trial_evidence(tmp_path: Path) -> None:
    failure = TrialFailure(
        category="infrastructure",
        kind="provider",
        message="provider failed",
        evidence={"request": {"attempt": 1}},
    )
    trial = _trial(status="error", failure=failure)
    result = load_run_result(
        _write(
            tmp_path,
            trials=[trial],
            complete=False,
            interruption={"kind": "crash", "message": "worker crashed"},
        )
    )

    loaded = result.trials[0]
    assert loaded.case == trial.case
    assert loaded.output == trial.output
    assert loaded.failure == failure
    assert loaded.trace == trial.trace
    assert loaded.judges == tuple(trial.judges)
    assert loaded.active_operation == trial.active_operation


def test_writer_sorts_trials_and_emits_one_versioned_shape(tmp_path: Path) -> None:
    second = _trial(
        nodeid="test_eval.py::test_agent[case-a-trial2]",
        trial_index=2,
        configured_trials=2,
    )
    first = _trial(configured_trials=2)

    result_path = _write(tmp_path, trials=[second, first])
    payload = json.loads(result_path.read_text())

    assert payload["schema_version"] == "kensa.result.v1"
    assert [trial["trial_index"] for trial in payload["trials"]] == [1, 2]
    assert [trial["trial_index"] for trial in payload["aggregates"][0]["trials"]] == [1, 2]


@pytest.mark.parametrize(
    ("contents", "cause"),
    [
        ("{", "malformed JSON"),
        ("[]", "JSON object"),
        ("{}", "missing schema_version"),
        ('{"schema_version":"kensa.result.v2"}', "unsupported schema version"),
    ],
)
def test_loader_rejects_malformed_unsupported_and_unversioned_input(
    tmp_path: Path,
    contents: str,
    cause: str,
) -> None:
    result_path = tmp_path / "invalid.json"
    result_path.write_text(contents)

    with pytest.raises(ValueError, match="Kensa result artifact") as exc_info:
        load_run_result(result_path)

    assert str(result_path) in str(exc_info.value)
    assert cause in str(exc_info.value)


def test_loader_rejects_missing_path_with_path_specific_error(tmp_path: Path) -> None:
    result_path = tmp_path / "missing.json"

    with pytest.raises(ValueError, match="Could not read") as exc_info:
        load_run_result(result_path)

    assert str(result_path) in str(exc_info.value)
    assert "read" in str(exc_info.value)


def test_models_are_strict_frozen_and_recursively_forbid_unknown_fields(tmp_path: Path) -> None:
    result_path = _write(tmp_path, trials=[_trial()])
    result = load_run_result(result_path)

    with pytest.raises(ValidationError, match="frozen_instance"):
        result.complete = False

    payload = json.loads(result_path.read_text())
    payload["aggregates"][0]["trials"][0]["unexpected"] = True
    result_path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="unexpected") as exc_info:
        load_run_result(result_path)

    assert str(result_path) in str(exc_info.value)
    assert "aggregates.0.trials.0.unexpected" in str(exc_info.value)


@pytest.mark.parametrize(
    "mutation",
    [
        "status_failure",
        "order",
        "duplicate",
        "duplicate_index",
        "aggregate_membership",
        "aggregate_identity",
        "aggregate_count",
        "aggregate_verdict",
        "failure_count",
        "summary_metric",
        "complete_interruption",
    ],
)
def test_loader_rejects_cross_section_inconsistency(
    tmp_path: Path,
    mutation: str,
) -> None:
    first = _trial(configured_trials=2)
    second = _trial(
        nodeid="test_eval.py::test_agent[case-a-trial2]",
        trial_index=2,
        configured_trials=2,
    )
    result_path = _write(tmp_path, trials=[first, second])
    payload = json.loads(result_path.read_text())

    if mutation == "status_failure":
        payload["trials"][0]["status"] = "fail"
    elif mutation == "order":
        payload["trials"].reverse()
    elif mutation == "duplicate":
        payload["trials"].append(payload["trials"][0])
    elif mutation == "duplicate_index":
        duplicate = {**payload["trials"][0], "nodeid": "test_eval.py::test_agent[duplicate]"}
        payload["trials"].append(duplicate)
    elif mutation == "aggregate_membership":
        payload["aggregates"][0]["trials"] = payload["aggregates"][0]["trials"][:1]
    elif mutation == "aggregate_identity":
        payload["aggregates"][0]["trials"][0]["case_id"] = "other-case"
    elif mutation == "aggregate_count":
        payload["aggregates"][0]["passed"] = 1
    elif mutation == "aggregate_verdict":
        payload["aggregates"][0]["verdict"] = "fail"
    elif mutation == "failure_count":
        payload["summary"]["error_counts"]["agent"] = 1
    elif mutation == "summary_metric":
        payload["summary"]["cost_latency"]["latency_mean_ms"] = 999.0
    elif mutation == "complete_interruption":
        payload["interruption"] = {"kind": "crash", "message": "worker crashed"}
    result_path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="Kensa result artifact") as exc_info:
        load_run_result(result_path)

    assert str(result_path) in str(exc_info.value)


def test_loader_requires_every_failure_category(tmp_path: Path) -> None:
    result_path = _write(tmp_path)
    payload = json.loads(result_path.read_text())
    del payload["summary"]["error_counts"]["unknown"]
    result_path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="error_counts") as exc_info:
        load_run_result(result_path)

    assert str(result_path) in str(exc_info.value)


def test_failed_validation_does_not_replace_existing_artifact(tmp_path: Path) -> None:
    result_path = _write(tmp_path, trials=[_trial()])
    original = result_path.read_bytes()
    duplicate = _trial()

    with pytest.raises(ValueError, match="duplicate"):
        write_run_artifacts(
            run_id="run-1",
            trials=[duplicate, duplicate],
            result_path=result_path,
            artifact_dir=tmp_path,
        )

    assert result_path.read_bytes() == original
