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

_V1_RESULT_FIXTURE = Path(__file__).parent / "fixtures" / "results" / "kensa-result-v1.json"


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


def test_result_v1_accepts_additive_structured_tool_calls(tmp_path: Path) -> None:
    trial = _trial()
    tool_call = {
        "sequence": 0,
        "name": "lookup",
        "arguments": {"customer_id": "cus_1"},
        "result": {"found": True},
        "arguments_recorded": True,
        "result_recorded": True,
        "status": "ok",
        "span_id": "span-1",
        "duration_ms": 1.5,
    }
    trial.trace["tools"] = ["lookup"]
    trial.trace["tool_calls"] = [tool_call]

    result = load_run_result(_write(tmp_path, trials=[trial]))

    assert result.schema_version == "kensa.result.v1"
    assert result.trials[0].trace["tools"] == ["lookup"]
    assert result.trials[0].trace["tool_calls"] == [tool_call]


def test_loader_keeps_checked_in_v1_artifact_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kensa.artifacts as artifacts
    import kensa.results as results

    def reject_current_derivation(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("v1 validation used mutable derivation code")

    monkeypatch.setattr(artifacts, "aggregate_trials", reject_current_derivation)
    monkeypatch.setattr(results, "run_summary", reject_current_derivation, raising=False)

    result = load_run_result(_V1_RESULT_FIXTURE)

    assert result.run_id == "compatibility-v1"
    assert result.aggregates[0].verdict == "pass"
    assert result.summary.pass_k_curve[0].value == 1.0


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
    ("mutation", "cause"),
    [
        ("zero_index", "greater than or equal to 1"),
        ("index_above_configured", "cannot exceed configured_trials"),
        ("inconsistent_configured", "inconsistent configured_trials"),
    ],
)
def test_loader_rejects_impossible_trial_coordinates(
    tmp_path: Path,
    mutation: str,
    cause: str,
) -> None:
    result_path = _write(
        tmp_path,
        trials=[
            _trial(configured_trials=2),
            _trial(
                nodeid="test_eval.py::test_agent[case-a-trial2]",
                trial_index=2,
                configured_trials=2,
            ),
        ],
    )
    payload = json.loads(result_path.read_text())

    if mutation == "zero_index":
        payload["trials"][0]["trial_index"] = 0
    elif mutation == "index_above_configured":
        payload["trials"][1]["trial_index"] = 3
    elif mutation == "inconsistent_configured":
        payload["trials"][1]["configured_trials"] = 3
    result_path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match=cause):
        load_run_result(result_path)


def test_loader_allows_filtered_noncontiguous_trial_indices(tmp_path: Path) -> None:
    result_path = _write(
        tmp_path,
        trials=[
            _trial(configured_trials=3),
            _trial(
                nodeid="test_eval.py::test_agent[case-a-trial3]",
                trial_index=3,
                configured_trials=3,
            ),
        ],
    )

    result = load_run_result(result_path)

    assert [trial.trial_index for trial in result.trials] == [1, 3]
    assert result.aggregates[0].partial is True


@pytest.mark.parametrize(
    ("trace", "expected_total", "expected_known", "expected_known_trials"),
    [
        (
            {"known_cost_usd": 0.1, "llm_turns": 1},
            None,
            0.1,
            0,
        ),
        (
            {"cost_usd": 0.2, "llm_turns": 1},
            0.2,
            0.2,
            1,
        ),
        (
            {"cost_usd": True, "llm_turns": 1},
            None,
            0.0,
            0,
        ),
    ],
)
def test_writer_retains_v1_legacy_cost_derivation(
    tmp_path: Path,
    trace: dict[str, Any],
    expected_total: float | None,
    expected_known: float,
    expected_known_trials: int,
) -> None:
    trial = _trial()
    trial.trace = trace

    summary = load_run_result(_write(tmp_path, trials=[trial])).summary.cost_latency

    assert summary.total_cost_usd == expected_total
    assert summary.known_cost_usd == expected_known
    assert summary.cost_known_trials == expected_known_trials


@pytest.mark.parametrize("llm_turns", [[], 10**400])
def test_loader_ignores_invalid_llm_turn_values(
    tmp_path: Path,
    llm_turns: Any,
) -> None:
    result_path = _write(tmp_path, trials=[_trial()])
    payload = json.loads(result_path.read_text())
    payload["trials"][0]["trace"]["llm_turns"] = llm_turns
    payload["aggregates"][0]["trials"][0]["trace"]["llm_turns"] = llm_turns
    payload["summary"]["cost_latency"]["mean_llm_turns"] = 0.0
    result_path.write_text(json.dumps(payload))

    result = load_run_result(result_path)

    assert result.summary.cost_latency.mean_llm_turns == 0.0


def test_loader_wraps_derivation_failures_with_artifact_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kensa.results as results

    result_path = _write(tmp_path, trials=[_trial()])

    def fail_derivation(trials: list[dict[str, Any]]) -> dict[str, Any]:
        del trials
        raise TypeError("invalid derived value")

    monkeypatch.setattr(results, "derive_v1_summary", fail_derivation)

    with pytest.raises(ValueError, match="derivation failed") as exc_info:
        load_run_result(result_path)

    assert str(result_path) in str(exc_info.value)
    assert "invalid derived value" in str(exc_info.value)


@pytest.mark.parametrize(
    ("case_id", "cause"),
    [
        ("case-b", "case.id must match case_id"),
        (["case-a"], "case.id must be a string"),
    ],
)
def test_loader_rejects_conflicting_or_nonstr_case_identity(
    tmp_path: Path,
    case_id: Any,
    cause: str,
) -> None:
    result_path = _write(tmp_path, trials=[_trial()])
    payload = json.loads(result_path.read_text())
    payload["trials"][0]["case"]["id"] = case_id
    result_path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match=cause) as exc_info:
        load_run_result(result_path)

    assert str(result_path) in str(exc_info.value)


def test_loader_allows_case_without_embedded_id(tmp_path: Path) -> None:
    result_path = _write(tmp_path, trials=[_trial()])
    payload = json.loads(result_path.read_text())
    del payload["trials"][0]["case"]["id"]
    del payload["aggregates"][0]["trials"][0]["case"]["id"]
    result_path.write_text(json.dumps(payload))

    result = load_run_result(result_path)

    assert result.trials[0].case_id == "case-a"
    assert "id" not in result.trials[0].case


def test_loader_rejects_zero_interruption_trial_index(tmp_path: Path) -> None:
    result_path = _write(
        tmp_path,
        trials=[_trial()],
        complete=False,
        interruption={
            "kind": "timeout",
            "message": "trial timed out",
            "trial_index": 1,
        },
    )
    payload = json.loads(result_path.read_text())
    payload["interruption"]["trial_index"] = 0
    result_path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="greater than or equal to 1"):
        load_run_result(result_path)


def test_loader_rejects_completed_provisional_trial(tmp_path: Path) -> None:
    result_path = _write(
        tmp_path,
        trials=[_trial(status="provisional")],
        complete=False,
    )
    payload = json.loads(result_path.read_text())
    payload["complete"] = True
    result_path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="complete result cannot contain provisional trials"):
        load_run_result(result_path)


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
        payload["aggregates"][0]["group_id"] = "other-group"
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
