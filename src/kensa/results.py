"""Strict public models for Kensa run-result artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from kensa._result_v1 import derive_v1_aggregates, derive_v1_summary
from kensa.errors import FailureCategory, TrialFailure

RunStatus: TypeAlias = Literal["pass", "fail", "error", "skipped", "provisional"]
AggregateVerdict: TypeAlias = Literal["pass", "fail", "flaky", "error", "partial"]
TrialPhase: TypeAlias = Literal["setup", "call", "teardown"]

_SCHEMA_VERSION = "kensa.result.v1"
_FAILURE_CATEGORIES = {
    "agent",
    "simulator",
    "judge",
    "configuration",
    "infrastructure",
    "harness",
    "unknown",
}
_FailureCount = Annotated[int, Field(ge=0)]


class ResultModel(BaseModel):
    """Strict immutable base for public result models."""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        allow_inf_nan=False,
    )


class RunInterruption(ResultModel):
    kind: str = Field(min_length=1)
    message: str
    nodeid: str | None = None
    case_id: str | None = None
    trial_index: int | None = Field(default=None, ge=1)
    phase: TrialPhase | None = None


class TrialResult(ResultModel):
    nodeid: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    trial_index: int = Field(ge=1)
    configured_trials: int = Field(ge=1)
    status: RunStatus
    case: dict[str, JsonValue]
    output: JsonValue | None
    failure: TrialFailure | None
    duration_ms: float = Field(ge=0)
    trace: dict[str, JsonValue]
    judges: tuple[dict[str, JsonValue], ...]
    active_operation: dict[str, JsonValue] | None
    smoke: bool

    @model_validator(mode="after")
    def _validate_failure(self) -> TrialResult:
        if "id" in self.case:
            case_id = self.case["id"]
            if not isinstance(case_id, str):
                raise ValueError("case.id must be a string")
            if case_id != self.case_id:
                raise ValueError("case.id must match case_id")
        if self.trial_index > self.configured_trials:
            raise ValueError("trial_index cannot exceed configured_trials")
        requires_failure = self.status in {"fail", "error", "skipped"}
        if requires_failure != (self.failure is not None):
            expectation = "one failure" if requires_failure else "failure=None"
            raise ValueError(f"trial status {self.status!r} requires {expectation}")
        return self


class AggregateResult(ResultModel):
    group_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    configured_trials: int = Field(ge=1)
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    errored: int = Field(ge=0)
    skipped: int = Field(ge=0)
    partial: bool
    verdict: AggregateVerdict
    trials: tuple[TrialResult, ...]
    smoke: bool


class ReliabilityPoint(ResultModel):
    k: int = Field(ge=1)
    value: float = Field(ge=0, le=1)
    cohorts: int = Field(ge=1)


class ReliabilityCohort(ResultModel):
    group_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    passed: int = Field(ge=0)
    total: int = Field(ge=1)


class CostLatencySummary(ResultModel):
    latency_p50_ms: float = Field(ge=0)
    latency_p95_ms: float = Field(ge=0)
    latency_mean_ms: float = Field(ge=0)
    total_cost_usd: float | None = Field(default=None, ge=0)
    known_cost_usd: float = Field(ge=0)
    cost_per_pass_usd: float | None = Field(default=None, ge=0)
    mean_llm_turns: float = Field(ge=0)
    cost_known_trials: int = Field(ge=0)
    cost_relevant_trials: int = Field(ge=0)
    cost_coverage: float = Field(ge=0, le=1)
    has_cost: bool
    cost_complete: bool
    cost_partial: bool


class RunSummary(ResultModel):
    pass_k_curve: tuple[ReliabilityPoint, ...]
    pass_k_cohorts: tuple[ReliabilityCohort, ...]
    eligible_agent_trials: int = Field(ge=0)
    error_counts: dict[FailureCategory, _FailureCount]
    excluded_error_trials: int = Field(ge=0)
    cost_latency: CostLatencySummary

    @model_validator(mode="after")
    def _validate_error_categories(self) -> RunSummary:
        if set(self.error_counts) != _FAILURE_CATEGORIES:
            raise ValueError("error_counts must contain every failure category exactly once")
        return self


class RunResult(ResultModel):
    schema_version: Literal["kensa.result.v1"]
    run_id: str = Field(min_length=1)
    complete: bool
    interruption: RunInterruption | None
    trials: tuple[TrialResult, ...]
    aggregates: tuple[AggregateResult, ...]
    summary: RunSummary

    @model_validator(mode="after")
    def _validate_contract(self) -> RunResult:
        if self.complete and self.interruption is not None:
            raise ValueError("complete result cannot contain an interruption")
        if self.complete and any(trial.status == "provisional" for trial in self.trials):
            raise ValueError("complete result cannot contain provisional trials")

        nodeids = [trial.nodeid for trial in self.trials]
        if len(nodeids) != len(set(nodeids)):
            raise ValueError("trials contain duplicate node IDs")

        trial_keys = [(trial.group_id, trial.trial_index) for trial in self.trials]
        if len(trial_keys) != len(set(trial_keys)):
            raise ValueError("trials contain duplicate group and trial indexes")

        configured_trials_by_group: dict[str, int] = {}
        for trial in self.trials:
            configured_trials = configured_trials_by_group.setdefault(
                trial.group_id, trial.configured_trials
            )
            if trial.configured_trials != configured_trials:
                raise ValueError("trials in one group have inconsistent configured_trials")

        expected_order = sorted(
            self.trials,
            key=lambda trial: (trial.group_id, trial.trial_index, trial.nodeid),
        )
        if list(self.trials) != expected_order:
            raise ValueError("trials are not in deterministic order")

        for aggregate in self.aggregates:
            if any(
                trial.group_id != aggregate.group_id or trial.case_id != aggregate.case_id
                for trial in aggregate.trials
            ):
                raise ValueError("aggregate contains a trial with mismatched identifiers")

        trial_payloads = [trial.model_dump(mode="json") for trial in self.trials]
        expected_aggregates = derive_v1_aggregates(trial_payloads)
        actual_aggregates = [aggregate.model_dump(mode="json") for aggregate in self.aggregates]
        if actual_aggregates != expected_aggregates:
            raise ValueError("aggregates do not match top-level trials")

        expected_summary = derive_v1_summary(trial_payloads)
        if self.summary.model_dump(mode="json") != expected_summary:
            raise ValueError("summary does not match top-level trials")
        return self


def load_run_result(path: str | Path) -> RunResult:
    """Load one complete, interrupted, or initial v1 result artifact."""
    result_path = Path(path)
    try:
        contents = result_path.read_text()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Could not read Kensa result artifact {result_path}: {exc}") from exc

    try:
        payload = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid Kensa result artifact {result_path}: malformed JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid Kensa result artifact {result_path}: expected a JSON object")
    if "schema_version" not in payload:
        raise ValueError(f"Invalid Kensa result artifact {result_path}: missing schema_version")
    if payload["schema_version"] != _SCHEMA_VERSION:
        raise ValueError(
            f"Invalid Kensa result artifact {result_path}: "
            f"unsupported schema version {payload['schema_version']!r}"
        )

    try:
        return RunResult.model_validate_json(contents)
    except ValidationError as exc:
        error = exc.errors(include_input=False, include_url=False)[0]
        location = ".".join(str(part) for part in error["loc"])
        cause = f"{location}: {error['msg']}" if location else str(error["msg"])
        raise ValueError(f"Invalid Kensa result artifact {result_path}: {cause}") from exc
    except Exception as exc:
        cause = str(exc).strip() or type(exc).__name__
        raise ValueError(
            f"Invalid Kensa result artifact {result_path}: derivation failed: {cause}"
        ) from exc


__all__ = [
    "AggregateResult",
    "AggregateVerdict",
    "CostLatencySummary",
    "ReliabilityCohort",
    "ReliabilityPoint",
    "ResultModel",
    "RunInterruption",
    "RunResult",
    "RunStatus",
    "RunSummary",
    "TrialPhase",
    "TrialResult",
    "load_run_result",
]
