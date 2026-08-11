"""Kensa eval result artifact helpers."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kensa._smoke import is_smoke_trial
from kensa.results import RunInterruption, RunResult, TrialResult
from kensa.runtime import TrialMetadata


@dataclass
class KensaAggregate:
    group_id: str
    case_id: str
    configured_trials: int
    total: int
    passed: int
    failed: int
    errored: int
    partial: bool
    verdict: str
    trials: list[TrialMetadata]
    skipped: int = 0
    smoke: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "case_id": self.case_id,
            "configured_trials": self.configured_trials,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
            "skipped": self.skipped,
            "partial": self.partial,
            "verdict": self.verdict,
            "trials": [trial.to_dict() for trial in self.trials],
            "smoke": self.smoke,
        }


def trial_sort_key(trial: TrialMetadata) -> tuple[str, int, str]:
    return trial.group_id, trial.trial_index, trial.nodeid


def upsert_trial(trials: list[TrialMetadata], metadata: TrialMetadata) -> None:
    for index, existing in enumerate(trials):
        if existing.nodeid == metadata.nodeid:
            trials[index] = metadata
            return
    trials.append(metadata)


def write_run_artifacts(
    *,
    run_id: str,
    trials: list[TrialMetadata],
    result_path: Path,
    artifact_dir: Path,
    core_result: Mapping[str, Any],
    complete: bool = True,
    interruption: dict[str, Any] | None = None,
) -> list[KensaAggregate]:
    ordered_trials = sorted(trials, key=trial_sort_key)
    trial_payloads = [
        TrialResult.model_validate_json(json.dumps(trial.to_dict(), allow_nan=False)).model_dump(
            mode="json"
        )
        for trial in ordered_trials
    ]
    payload = dict(core_result)
    result = RunResult.model_validate_json(json.dumps(payload, allow_nan=False))
    expected_interruption = (
        RunInterruption.model_validate(interruption) if interruption is not None else None
    )
    expected_trials = tuple(
        (
            trial_payload["nodeid"],
            trial_payload["group_id"],
            trial_payload["case_id"],
            trial_payload["trial_index"],
            trial_payload["configured_trials"],
            trial_payload["status"],
        )
        for trial_payload in trial_payloads
    )
    actual_trials = tuple(
        (
            trial.nodeid,
            trial.group_id,
            trial.case_id,
            trial.trial_index,
            trial.configured_trials,
            trial.status,
        )
        for trial in result.trials
    )
    contradictions: list[str] = []
    if result.run_id != run_id:
        contradictions.append("run_id")
    if result.complete is not complete:
        contradictions.append("complete")
    if result.interruption != expected_interruption:
        contradictions.append("interruption")
    if actual_trials != expected_trials:
        contradictions.append("trials")
    if contradictions:
        raise ValueError(
            "Kensa core result contradicts the requested artifact: "
            + ", ".join(contradictions)
        )
    _write_text_atomic(result_path, result.model_dump_json(indent=2))
    result_trials = [trial_result_to_metadata(trial) for trial in result.trials]
    _write_trace_artifact(run_id, result_trials, artifact_dir)
    return _metadata_aggregates(result, ordered_trials)


def aggregates_from_core_result(
    payload: Mapping[str, Any],
    trials: list[TrialMetadata],
) -> list[KensaAggregate]:
    result = RunResult.model_validate_json(json.dumps(dict(payload), allow_nan=False))
    return _metadata_aggregates(result, sorted(trials, key=trial_sort_key))


def _metadata_aggregates(
    result: RunResult,
    trials: list[TrialMetadata],
) -> list[KensaAggregate]:
    by_nodeid = {trial.nodeid: trial for trial in trials}
    return [
        KensaAggregate(
            group_id=aggregate.group_id,
            case_id=aggregate.case_id,
            configured_trials=aggregate.configured_trials,
            total=aggregate.total,
            passed=aggregate.passed,
            failed=aggregate.failed,
            errored=aggregate.errored,
            skipped=aggregate.skipped,
            partial=aggregate.partial,
            verdict=aggregate.verdict,
            trials=[by_nodeid[trial.nodeid] for trial in aggregate.trials],
            smoke=aggregate.smoke,
        )
        for aggregate in result.aggregates
    ]


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_text_atomic(path, json.dumps(payload, indent=2))


def _write_trace_artifact(
    run_id: str,
    trials: list[TrialMetadata],
    artifact_dir: Path,
) -> None:
    rows = [_trial_trace_record(run_id, trial) for trial in trials if trial.case]
    if not rows:
        return
    output = artifact_dir / "traces" / "runs" / run_id / "trials.jsonl"
    content = "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n"
    _write_text_atomic(output, content)


def _trial_trace_record(run_id: str, trial: TrialMetadata) -> dict[str, Any]:
    trace = trial.trace if isinstance(trial.trace, dict) else {}
    spans = trace.get("spans") if isinstance(trace.get("spans"), list) else []
    agent_runs = trace.get("agent_runs") if isinstance(trace.get("agent_runs"), list) else []
    return {
        "id": f"{run_id}_{trial.case_id}_trial{trial.trial_index}",
        "run_id": run_id,
        "case_id": trial.case_id,
        "case": trial.case,
        "output": trial.output,
        "status": trial.status,
        "failure": (trial.failure.model_dump(mode="json") if trial.failure is not None else None),
        "smoke": trial.is_smoke,
        "duration_ms": trial.duration_ms,
        "spans": spans,
        "agent_runs": agent_runs,
        "incomplete": bool(trace.get("incomplete", False)),
        "incomplete_reason": trace.get("incomplete_reason"),
    }


def trial_from_dict(row: dict[str, Any]) -> TrialMetadata:
    result = TrialResult.model_validate_json(json.dumps(row, allow_nan=False))
    return trial_result_to_metadata(result)


def trial_result_to_metadata(result: TrialResult) -> TrialMetadata:
    return TrialMetadata(
        nodeid=result.nodeid,
        group_id=result.group_id,
        case_id=result.case_id,
        trial_index=result.trial_index,
        configured_trials=result.configured_trials,
        status=result.status,
        case=result.case,
        output=result.output,
        failure=result.failure,
        duration_ms=result.duration_ms,
        trace=result.trace,
        judges=list(result.judges),
        active_operation=result.active_operation,
        smoke=is_smoke_trial(result.model_dump(mode="json")),
    )


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            handle.write(content)
            temporary_path = Path(handle.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


__all__ = [
    "KensaAggregate",
    "trial_from_dict",
    "trial_result_to_metadata",
    "trial_sort_key",
    "upsert_trial",
    "write_json_atomic",
    "write_run_artifacts",
]
