"""Kensa eval result artifact helpers."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kensa._smoke import is_smoke_trial
from kensa.runtime import TrialMetadata
from kensa.scoring import run_summary


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


def aggregate_trials(trials: list[TrialMetadata]) -> list[KensaAggregate]:
    groups: dict[str, list[TrialMetadata]] = {}
    for trial in trials:
        groups.setdefault(trial.group_id, []).append(trial)
    aggregates: list[KensaAggregate] = []
    for group_id, group_trials in sorted(groups.items()):
        all_trials = sorted(group_trials, key=lambda trial: trial.trial_index)
        ordered = [trial for trial in all_trials if trial.status != "skipped"]
        if not ordered:
            continue
        total = len(ordered)
        passed = sum(1 for trial in ordered if trial.status == "pass")
        errored = sum(1 for trial in ordered if trial.status == "error")
        failed = sum(1 for trial in ordered if trial.status == "fail")
        skipped = len(all_trials) - total
        configured = max(trial.configured_trials for trial in all_trials)
        partial = total + skipped < configured
        timed_out = any(trial.error_kind == "timeout" for trial in ordered)
        if timed_out:
            verdict = "error"
        elif partial:
            verdict = "partial"
        elif errored:
            verdict = "error"
        elif passed == total:
            verdict = "pass"
        elif failed == total:
            verdict = "fail"
        else:
            verdict = "flaky"
        aggregates.append(
            KensaAggregate(
                group_id=group_id,
                case_id=ordered[0].case_id,
                configured_trials=configured,
                total=total,
                passed=passed,
                failed=failed,
                errored=errored,
                partial=partial,
                verdict=verdict,
                trials=ordered,
                skipped=skipped,
                smoke=any(trial.is_smoke for trial in all_trials),
            )
        )
    return aggregates


def upsert_trial(trials: list[TrialMetadata], metadata: TrialMetadata) -> None:
    for index, existing in enumerate(trials):
        if existing.nodeid == metadata.nodeid:
            trials[index] = metadata
            return
    trials.append(metadata)


def load_trials(result_path: Path) -> list[TrialMetadata]:
    payload = json.loads(result_path.read_text())
    rows = payload.get("trials", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        raise ValueError(f"Kensa result artifact has invalid trials: {result_path}")
    return [trial_from_dict(row) for row in rows if isinstance(row, dict)]


def write_run_artifacts(
    *,
    run_id: str,
    trials: list[TrialMetadata],
    result_path: Path,
    artifact_dir: Path,
    complete: bool = True,
    interruption: dict[str, Any] | None = None,
) -> list[KensaAggregate]:
    aggregates = aggregate_trials(trials)
    payload = {
        "run_id": run_id,
        "complete": complete,
        "interruption": interruption,
        "trials": [trial.to_dict() for trial in trials],
        "aggregates": [aggregate.to_dict() for aggregate in aggregates],
    }
    payload["summary"] = run_summary(payload)
    write_json_atomic(result_path, payload)
    _write_trace_artifact(run_id, trials, artifact_dir)
    return aggregates


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
    return {
        "id": f"{run_id}_{trial.case_id}_trial{trial.trial_index}",
        "run_id": run_id,
        "case_id": trial.case_id,
        "case": trial.case,
        "output": trial.output,
        "status": trial.status,
        "smoke": trial.is_smoke,
        "duration_ms": trial.duration_ms,
        "spans": spans,
        "incomplete": bool(trace.get("incomplete", False)),
        "incomplete_reason": trace.get("incomplete_reason"),
    }


def trial_from_dict(row: dict[str, Any]) -> TrialMetadata:
    case = row.get("case")
    trace = row.get("trace")
    judges = row.get("judges")
    active_operation = row.get("active_operation")
    return TrialMetadata(
        nodeid=str(row.get("nodeid", "")),
        group_id=str(row.get("group_id", "")),
        case_id=str(row.get("case_id", "default")),
        trial_index=int(row.get("trial_index", 1)),
        configured_trials=int(row.get("configured_trials", 1)),
        status=str(row.get("status", "error")),
        case=case if isinstance(case, dict) else {},
        output=row.get("output"),
        error=str(row["error"]) if row.get("error") is not None else None,
        error_kind=(str(row["error_kind"]) if row.get("error_kind") is not None else None),
        duration_ms=float(row.get("duration_ms", 0.0)),
        trace=trace if isinstance(trace, dict) else {},
        judges=[judge for judge in judges if isinstance(judge, dict)]
        if isinstance(judges, list)
        else [],
        active_operation=active_operation if isinstance(active_operation, dict) else None,
        smoke=is_smoke_trial(row),
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
    "aggregate_trials",
    "load_trials",
    "trial_from_dict",
    "trial_sort_key",
    "upsert_trial",
    "write_json_atomic",
    "write_run_artifacts",
]
