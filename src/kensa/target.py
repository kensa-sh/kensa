"""Target-owned external run evidence."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

_MODEL_CONFIG = ConfigDict(
    frozen=True,
    extra="forbid",
    str_strip_whitespace=True,
    allow_inf_nan=False,
)
_NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]


def _nonblank(value: str | None) -> str | None:
    if value is not None and not value:
        raise ValueError("must contain non-whitespace text")
    return value


class EffectPolicy(StrEnum):
    NONE = "none"
    CAPTURED = "captured"
    SANDBOXED = "sandboxed"
    LIVE = "live"


class EvidenceCompleteness(StrEnum):
    COMPLETE = "complete"
    PENDING = "pending"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class TraceReference(BaseModel):
    """Optional provider trace provenance."""

    model_config = _MODEL_CONFIG

    provider: str
    trace_id: str
    url: str | None = None

    _validate_identity = field_validator("provider", "trace_id")(_nonblank)


class ExecutionAttestation(BaseModel):
    """Target-supplied execution facts."""

    model_config = _MODEL_CONFIG

    revision: str
    environment: str
    effects: EffectPolicy

    _validate_identity = field_validator("revision", "environment")(_nonblank)


class AgentEvent(BaseModel):
    """One target-reported trajectory event."""

    model_config = _MODEL_CONFIG

    id: str
    parent_id: str | None = None
    sequence: _NonNegativeInt
    kind: Literal["llm", "tool", "handoff", "retrieval", "action", "state", "span"]
    name: str
    input: JsonValue | None = None
    output: JsonValue | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    status: Literal["completed", "failed", "cancelled"]
    started_at_ns: _NonNegativeInt | None = None
    ended_at_ns: _NonNegativeInt | None = None

    _validate_identity = field_validator("id", "parent_id", "name")(_nonblank)

    @model_validator(mode="after")
    def _validate_timestamps(self) -> Self:
        if (
            self.started_at_ns is not None
            and self.ended_at_ns is not None
            and self.ended_at_ns < self.started_at_ns
        ):
            raise ValueError("ended_at_ns must be greater than or equal to started_at_ns")
        return self


class StateObservation(BaseModel):
    """Actual state observed in a target-owned environment."""

    model_config = _MODEL_CONFIG

    name: str
    value: JsonValue
    source: str
    observed_at_ns: _NonNegativeInt | None = None

    _validate_identity = field_validator("name", "source")(_nonblank)


class AgentRunEvidence(BaseModel):
    """Immutable evidence supplied for one target-owned agent run."""

    model_config = _MODEL_CONFIG

    schema_version: Literal["kensa.agent_run.v1"] = "kensa.agent_run.v1"
    run_id: str
    attestation: ExecutionAttestation
    events: tuple[AgentEvent, ...] = Field(default_factory=tuple)
    trace: TraceReference | None = None
    trajectory_completeness: EvidenceCompleteness
    state: tuple[StateObservation, ...] = Field(default_factory=tuple)
    state_completeness: EvidenceCompleteness
    incomplete_reason: str | None = None

    _validate_identity = field_validator("run_id", "incomplete_reason")(_nonblank)

    @model_validator(mode="after")
    def _validate_run(self) -> Self:
        event_ids: set[str] = set()
        previous_sequence: int | None = None
        for event in self.events:
            if event.id in event_ids:
                raise ValueError("event IDs must be unique within a run")
            event_ids.add(event.id)
            if previous_sequence is not None and event.sequence <= previous_sequence:
                raise ValueError("event sequences must be strictly increasing")
            previous_sequence = event.sequence
            if event.parent_id == event.id:
                raise ValueError("an event cannot name itself as its parent")

        complete = (
            self.trajectory_completeness == EvidenceCompleteness.COMPLETE
            and self.state_completeness == EvidenceCompleteness.COMPLETE
        )
        if complete and self.incomplete_reason is not None:
            raise ValueError("incomplete_reason must be absent when evidence is complete")
        if not complete and self.incomplete_reason is None:
            raise ValueError("incomplete_reason is required when evidence is incomplete")
        return self


def attach_agent_run(evidence: AgentRunEvidence) -> None:
    """Attach evidence while the current case operation is active."""

    from kensa.runtime import current_runtime

    runtime = current_runtime()
    if runtime is None:
        return
    runtime.attach_agent_run(evidence)


__all__ = [
    "AgentEvent",
    "AgentRunEvidence",
    "EffectPolicy",
    "EvidenceCompleteness",
    "ExecutionAttestation",
    "StateObservation",
    "TraceReference",
    "attach_agent_run",
]
