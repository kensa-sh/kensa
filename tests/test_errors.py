from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

import kensa
from kensa.errors import FailureCategory, KensaEvalError, TrialFailure
from kensa.pytest import (
    FailureCategory as PublicFailureCategory,
)
from kensa.pytest import (
    KensaEvalError as PublicKensaEvalError,
)
from kensa.pytest import (
    TrialFailure as PublicTrialFailure,
)


@pytest.mark.parametrize(
    "category",
    [
        "agent",
        "simulator",
        "judge",
        "configuration",
        "infrastructure",
        "harness",
        "unknown",
    ],
)
def test_eval_error_validates_and_snapshots_failure(category: FailureCategory) -> None:
    evidence: dict[str, Any] = {"nested": {"values": [1, True, None]}}

    error = KensaEvalError(
        " provider unavailable ",
        category=category,
        kind=" execution ",
        evidence=cast(Any, evidence),
    )
    cast(list[Any], evidence["nested"]["values"]).append("changed")

    assert error.failure == TrialFailure(
        category=category,
        kind="execution",
        message="provider unavailable",
        evidence={"nested": {"values": [1, True, None]}},
    )
    assert str(error) == "provider unavailable"
    assert PublicFailureCategory is FailureCategory
    assert PublicTrialFailure is TrialFailure
    assert PublicKensaEvalError is KensaEvalError
    assert not hasattr(kensa, "TrialFailure")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", "other"),
        ("kind", " "),
        ("message", ""),
        ("evidence", {"bad": object()}),
        ("evidence", {"bad": float("nan")}),
        ("evidence", {"bad": float("inf")}),
    ],
)
def test_eval_error_rejects_invalid_failure(field: str, value: Any) -> None:
    arguments: dict[str, Any] = {
        "message": "failed",
        "category": "agent",
        "kind": "execution",
        "evidence": {},
    }
    arguments[field] = value

    with pytest.raises(ValidationError):
        KensaEvalError(**arguments)


def test_trial_failure_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        TrialFailure.model_validate(
            {
                "category": "agent",
                "kind": "execution",
                "message": "failed",
                "evidence": {},
                "unexpected": True,
            }
        )
