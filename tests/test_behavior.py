from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from kensa import (
    BEHAVIOR_CANDIDATE_SCHEMA_VERSION,
    BEHAVIOR_FINGERPRINT_VERSION,
    BehaviorCandidate,
    behavior_candidate_schema,
    behavior_semantic_fingerprint,
)
from kensa.models import InspectIdea


def _idea(**overrides: object) -> InspectIdea:
    payload: dict[str, object] = {
        "id": "tool-loop-on-empty-results",
        "trace_ids": ["trace-one"],
        "source": "local",
        "status": "pending",
        "failure_pattern": "Agent loops after empty results",
        "expected_outcome": "Agent stops after two empty results",
        "expected_current_behavior": "fail",
        "proposed_checks": ["max_turns", "no_repeat_calls"],
        "case_shape": "query with no matches",
        "risks": "requires deterministic search results",
    }
    payload.update(overrides)
    return InspectIdea.model_validate(payload)


def test_behavior_candidate_from_inspect_idea_publishes_versioned_contract() -> None:
    candidate = BehaviorCandidate.from_inspect_idea(_idea())

    assert candidate.schema_version == BEHAVIOR_CANDIDATE_SCHEMA_VERSION
    assert candidate.fingerprint_version == BEHAVIOR_FINGERPRINT_VERSION
    assert candidate.id == "tool-loop-on-empty-results"
    assert candidate.trace_ids == ("trace-one",)
    assert candidate.semantic_fingerprint.startswith("sha256:")
    assert behavior_candidate_schema()["properties"]["schema_version"]["const"] == (
        BEHAVIOR_CANDIDATE_SCHEMA_VERSION
    )


def test_behavior_candidate_rejects_unknown_version_and_mismatched_fingerprint() -> None:
    payload = BehaviorCandidate.from_inspect_idea(_idea()).model_dump(mode="json")

    wrong_version = deepcopy(payload)
    wrong_version["schema_version"] = "kensa.behavior_candidate.v2"
    with pytest.raises(ValidationError, match="schema_version"):
        BehaviorCandidate.model_validate(wrong_version)

    wrong_fingerprint = deepcopy(payload)
    wrong_fingerprint["semantic_fingerprint"] = "sha256:" + "0" * 64
    with pytest.raises(ValidationError, match="does not match"):
        BehaviorCandidate.model_validate(wrong_fingerprint)


def test_behavior_candidate_rejects_blank_trace_id() -> None:
    payload = BehaviorCandidate.from_inspect_idea(_idea()).model_dump(mode="json")
    payload["trace_ids"] = [" "]

    with pytest.raises(ValidationError, match="trace_ids entries must be non-empty"):
        BehaviorCandidate.model_validate(payload)


def test_semantic_fingerprint_is_stable_across_runs_and_machines() -> None:
    first = BehaviorCandidate.from_inspect_idea(_idea())
    second = BehaviorCandidate.from_inspect_idea(
        _idea(
            id="agent-retries-empty-search",
            trace_ids=["different-machine-trace"],
            source="otlp",
            status="approved",
            expected_current_behavior="pass",
            proposed_checks=["stop_after_empty_result"],
            failure_pattern="  AGENT LOOPS AFTER EMPTY RESULTS  ",
            expected_outcome="Agent stops after two\nempty results",
            case_shape="a lookup whose normalized response is empty",
            risks="different runtime",
        )
    )

    assert first.semantic_fingerprint == second.semantic_fingerprint


def test_semantic_fingerprint_changes_with_behavior_class() -> None:
    first = BehaviorCandidate.from_inspect_idea(_idea())
    different = BehaviorCandidate.from_inspect_idea(
        _idea(
            failure_pattern="Agent retries after a rate limit",
            expected_outcome="Agent waits before retrying",
        )
    )

    assert first.semantic_fingerprint != different.semantic_fingerprint
    assert behavior_semantic_fingerprint(
        failure_pattern="  AGENT LOOPS AFTER EMPTY RESULTS ",
        expected_outcome="Agent stops after two\nempty results",
    ) == behavior_semantic_fingerprint(
        failure_pattern="agent loops after empty results",
        expected_outcome="agent stops after two empty results",
    )
