"""Versioned behavior-candidate contract."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from kensa.models import ExpectedCurrentBehavior, InspectIdea, KensaModel

BEHAVIOR_CANDIDATE_SCHEMA_VERSION = "kensa.behavior_candidate.v1"
BEHAVIOR_FINGERPRINT_VERSION = "kensa.behavior_fingerprint.v1"


class BehaviorCandidate(KensaModel):
    """Portable description of one observed behavior worth evaluating."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["kensa.behavior_candidate.v1"]
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    trace_ids: tuple[str, ...] = Field(min_length=1)
    source: str = Field(min_length=1)
    failure_pattern: str = Field(min_length=1)
    expected_outcome: str = Field(min_length=1)
    expected_current_behavior: ExpectedCurrentBehavior
    proposed_checks: tuple[str, ...] = ()
    case_shape: str | None = None
    risks: str | None = None
    fingerprint_version: Literal["kensa.behavior_fingerprint.v1"]
    semantic_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("trace_ids")
    @classmethod
    def _non_empty_trace_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not trace_id.strip() for trace_id in value):
            raise ValueError("trace_ids entries must be non-empty")
        return value

    @model_validator(mode="after")
    def _valid_semantic_fingerprint(self) -> Self:
        expected = behavior_semantic_fingerprint(
            failure_pattern=self.failure_pattern,
            expected_outcome=self.expected_outcome,
        )
        if self.semantic_fingerprint != expected:
            raise ValueError("semantic_fingerprint does not match the behavior fields")
        return self

    @classmethod
    def from_inspect_idea(cls, idea: InspectIdea) -> Self:
        """Create the public contract from an inspect-queue item."""
        return cls(
            schema_version=BEHAVIOR_CANDIDATE_SCHEMA_VERSION,
            id=idea.id,
            trace_ids=tuple(idea.trace_ids),
            source=idea.source,
            failure_pattern=idea.failure_pattern,
            expected_outcome=idea.expected_outcome,
            expected_current_behavior=idea.expected_current_behavior,
            proposed_checks=tuple(idea.proposed_checks),
            case_shape=idea.case_shape,
            risks=idea.risks,
            fingerprint_version=BEHAVIOR_FINGERPRINT_VERSION,
            semantic_fingerprint=behavior_semantic_fingerprint(
                failure_pattern=idea.failure_pattern,
                expected_outcome=idea.expected_outcome,
            ),
        )


def behavior_semantic_fingerprint(
    *,
    failure_pattern: str,
    expected_outcome: str,
) -> str:
    """Return a deterministic fingerprint for normalized behavior semantics."""
    payload = {
        "expected_outcome": _normalize_semantic_text(expected_outcome),
        "failure_pattern": _normalize_semantic_text(failure_pattern),
        "fingerprint_version": BEHAVIOR_FINGERPRINT_VERSION,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def behavior_candidate_schema() -> dict[str, Any]:
    """Return the JSON Schema for the current behavior-candidate contract."""
    return BehaviorCandidate.model_json_schema()


def _normalize_semantic_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


__all__ = [
    "BEHAVIOR_CANDIDATE_SCHEMA_VERSION",
    "BEHAVIOR_FINGERPRINT_VERSION",
    "BehaviorCandidate",
    "behavior_candidate_schema",
    "behavior_semantic_fingerprint",
]
