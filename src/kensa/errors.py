"""Shared Kensa exception types."""

from __future__ import annotations

from copy import deepcopy
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue

FailureCategory: TypeAlias = Literal[
    "agent",
    "simulator",
    "judge",
    "configuration",
    "infrastructure",
    "harness",
    "unknown",
]


class TrialFailure(BaseModel):
    """Structured provenance for one non-passing trial."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )

    category: FailureCategory
    kind: str = Field(min_length=1)
    message: str = Field(min_length=1)
    evidence: dict[str, JsonValue] = Field(default_factory=dict)


class KensaEvalError(RuntimeError):
    """Explicitly attributed eval failure."""

    failure: TrialFailure

    def __init__(
        self,
        message: str,
        *,
        category: FailureCategory,
        kind: str,
        evidence: dict[str, JsonValue] | None = None,
    ) -> None:
        self.failure = TrialFailure(
            category=category,
            kind=kind,
            message=message,
            evidence=deepcopy(evidence) if evidence is not None else {},
        )
        super().__init__(self.failure.message)


class KensaCaseError(Exception):
    """Raised when a Kensa case contract is violated."""


class KensaTimeoutError(TimeoutError):
    """Raised when a bounded Kensa operation times out."""


__all__ = [
    "FailureCategory",
    "KensaCaseError",
    "KensaEvalError",
    "KensaTimeoutError",
    "TrialFailure",
]
