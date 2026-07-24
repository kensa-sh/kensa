from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from kensa import cli
from kensa._smoke import is_smoke_aggregate, is_smoke_identity, is_smoke_trial
from kensa.artifacts import aggregate_trials, write_run_artifacts
from kensa.pytest_plugin import _write_scoring_summary
from kensa.runtime import TrialMetadata
from kensa.scoring import (
    _percentile,
    cost_latency,
    pass_hat_k,
    pass_k_curve,
    run_summary,
)


def _trial(
    *,
    status: str,
    case_id: str = "case-a",
    group_id: str | None = None,
    duration_ms: float | None = 100,
    cost_usd: float | None = 0.01,
    llm_turns: float | None = 2,
    cost_available: bool | None = True,
    smoke: bool | None = False,
    case_smoke: bool = False,
    error_kind: str | None = None,
) -> dict[str, Any]:
    case: dict[str, Any] = {"id": case_id}
    if case_smoke:
        case["smoke"] = True
    trace = {
        key: value
        for key, value in {"cost_usd": cost_usd, "llm_turns": llm_turns}.items()
        if value is not None
    }
    if cost_available is not None:
        trace["cost_available"] = cost_available
    trial: dict[str, Any] = {
        "case": case,
        "case_id": case_id,
        "group_id": group_id or case_id,
        "status": status,
        "error_kind": error_kind,
        "duration_ms": duration_ms,
        "trace": trace,
    }
    if smoke is not None:
        trial["smoke"] = smoke
    return trial


def test_pass_hat_k_returns_known_reliability_values() -> None:
    assert pass_hat_k(3, 4, 1) == pytest.approx(0.75)
    assert pass_hat_k(3, 4, 2) == pytest.approx(0.5)
    assert pass_hat_k(3, 4, 3) == pytest.approx(0.25)
    assert pass_hat_k(3, 4, 4) == 0.0
    assert pass_hat_k(1, 1, 0) is None
    assert pass_hat_k(1, 1, 2) is None


def test_pass_k_curve_reports_changing_cohort_population() -> None:
    assert pass_k_curve([]) == []
    assert pass_k_curve([(2, 3), (1, 1)]) == [
        {"k": 1, "value": pytest.approx(5 / 6), "cohorts": 2},
        {"k": 2, "value": pytest.approx(1 / 3), "cohorts": 1},
        {"k": 3, "value": 0.0, "cohorts": 1},
    ]


def test_percentile_handles_empty_single_and_boundary_values() -> None:
    assert _percentile([], 95) == 0.0
    assert _percentile([7], 95) == 7
    assert _percentile([30, 10, 20], 0) == 10
    assert _percentile([30, 10, 20], 100) == 30


def test_cost_latency_reports_complete_costs() -> None:
    trials = [
        _trial(status="pass", duration_ms=10, cost_usd=0.1, llm_turns=2),
        _trial(status="fail", duration_ms=20, cost_usd=0.2, llm_turns=4),
        {
            "status": "error",
            "duration_ms": None,
            "trace": None,
        },
    ]

    assert cost_latency(trials) == {
        "latency_p50_ms": 15,
        "latency_p95_ms": 20,
        "latency_mean_ms": 15,
        "total_cost_usd": pytest.approx(0.3),
        "known_cost_usd": pytest.approx(0.3),
        "cost_per_pass_usd": pytest.approx(0.3),
        "mean_llm_turns": 3,
        "cost_known_trials": 2,
        "cost_relevant_trials": 2,
        "cost_coverage": 1.0,
        "has_cost": True,
        "cost_complete": True,
        "cost_partial": False,
    }


def test_cost_latency_never_presents_partial_cost_as_total() -> None:
    partial = cost_latency(
        [
            _trial(status="pass", cost_usd=0.1),
            _trial(status="fail", cost_usd=None, cost_available=False),
        ]
    )

    assert partial["total_cost_usd"] is None
    assert partial["known_cost_usd"] == 0.1
    assert partial["cost_per_pass_usd"] is None
    assert partial["cost_known_trials"] == 1
    assert partial["cost_relevant_trials"] == 2
    assert partial["cost_coverage"] == 0.5
    assert partial["has_cost"] is True
    assert partial["cost_complete"] is False
    assert partial["cost_partial"] is True

    unavailable = cost_latency([_trial(status="pass", cost_usd=0.0, cost_available=False)])
    assert unavailable["total_cost_usd"] is None
    assert unavailable["has_cost"] is False
    assert unavailable["cost_relevant_trials"] == 1

    explicit_zero = cost_latency([_trial(status="pass", cost_usd=0.0, cost_available=True)])
    assert explicit_zero["total_cost_usd"] == 0.0
    assert explicit_zero["cost_per_pass_usd"] == 0.0
    assert explicit_zero["cost_complete"] is True


def test_cost_latency_preserves_partial_cost_within_one_trial() -> None:
    trial = _trial(status="pass", cost_usd=None, cost_available=False)
    trial["trace"]["known_cost_usd"] = 0.2

    summary = cost_latency([trial])

    assert summary["total_cost_usd"] is None
    assert summary["known_cost_usd"] == 0.2
    assert summary["cost_per_pass_usd"] is None
    assert summary["cost_known_trials"] == 0
    assert summary["cost_relevant_trials"] == 1
    assert summary["cost_coverage"] == 0.0
    assert summary["has_cost"] is True
    assert summary["cost_complete"] is False
    assert summary["cost_partial"] is True

    trial["trace"].pop("cost_available")
    assert cost_latency([trial])["known_cost_usd"] == 0.2


def test_cost_latency_treats_active_llm_timeout_as_unknown_cost() -> None:
    timed_out = _trial(
        status="error",
        case_id="timeout",
        cost_usd=None,
        llm_turns=0,
        cost_available=False,
        error_kind="timeout",
    )
    timed_out["active_operation"] = {
        "name": "llm.call",
        "kind": "llm",
        "attributes": {},
    }

    summary = cost_latency([_trial(status="pass", cost_usd=0.1), timed_out])

    assert summary["total_cost_usd"] is None
    assert summary["known_cost_usd"] == 0.1
    assert summary["cost_known_trials"] == 1
    assert summary["cost_relevant_trials"] == 2
    assert summary["cost_coverage"] == 0.5
    assert summary["cost_complete"] is False
    assert summary["cost_partial"] is True

    timed_out["active_operation"]["kind"] = "tool"
    assert cost_latency([timed_out])["cost_relevant_trials"] == 0


def test_cost_latency_handles_legacy_and_invalid_cost_metadata() -> None:
    legacy_priced = _trial(
        status="pass",
        cost_usd=0.2,
        cost_available=None,
    )
    legacy_unknown = _trial(
        status="fail",
        cost_usd=0.0,
        cost_available=None,
    )
    invalid = [
        _trial(status="error", cost_usd=float("nan"), cost_available=True),
        _trial(status="error", cost_usd=-1, cost_available=True),
        _trial(status="error", cost_usd=True, cost_available=True),
    ]

    summary = cost_latency([legacy_priced, legacy_unknown, *invalid])

    assert summary["known_cost_usd"] == 0.2
    assert summary["cost_known_trials"] == 1
    assert summary["cost_relevant_trials"] == 5
    assert summary["cost_complete"] is False


def test_run_summary_keeps_pytest_groups_as_distinct_cohorts() -> None:
    summary = run_summary(
        {
            "trials": [
                _trial(status="pass", case_id="shared", group_id="mode-a"),
                _trial(status="pass", case_id="shared", group_id="mode-a"),
                _trial(status="pass", case_id="shared", group_id="mode-b"),
                _trial(status="fail", case_id="shared", group_id="mode-b"),
            ]
        }
    )

    assert summary["pass_k_cohorts"] == [
        {
            "group_id": "mode-a",
            "case_id": "shared",
            "passed": 2,
            "total": 2,
        },
        {
            "group_id": "mode-b",
            "case_id": "shared",
            "passed": 1,
            "total": 2,
        },
    ]
    assert summary["pass_k_curve"] == [
        {"k": 1, "value": 0.75, "cohorts": 2},
        {"k": 2, "value": 0.5, "cohorts": 2},
    ]


def test_run_summary_counts_agent_errors_and_timeouts_as_failures() -> None:
    summary = run_summary(
        {
            "trials": [
                _trial(status="pass", group_id="agent"),
                _trial(status="error", group_id="agent", error_kind="exception"),
                _trial(status="error", group_id="agent", error_kind="timeout"),
                _trial(
                    status="error",
                    group_id="agent",
                    error_kind="infrastructure",
                ),
                _trial(status="skipped", group_id="agent"),
            ]
        }
    )

    assert summary["eligible_agent_trials"] == 3
    assert summary["pass_k_cohorts"] == [
        {
            "group_id": "agent",
            "case_id": "case-a",
            "passed": 1,
            "total": 3,
        }
    ]
    assert summary["pass_k_curve"] == [
        {"k": 1, "value": pytest.approx(1 / 3), "cohorts": 1},
        {"k": 2, "value": 0.0, "cohorts": 1},
        {"k": 3, "value": 0.0, "cohorts": 1},
    ]


def test_aggregate_trials_excludes_skipped_trials() -> None:
    skipped = TrialMetadata(
        nodeid="test_eval.py::test_agent[trial1-case-a]",
        group_id="test_eval.py::test_agent[case-a]",
        case_id="case-a",
        trial_index=1,
        configured_trials=2,
        status="skipped",
        error_kind="skip",
    )
    passed = TrialMetadata(
        nodeid="test_eval.py::test_agent[trial2-case-a]",
        group_id="test_eval.py::test_agent[case-a]",
        case_id="case-a",
        trial_index=2,
        configured_trials=2,
        status="pass",
    )

    aggregates = aggregate_trials([skipped, passed])

    assert len(aggregates) == 1
    assert aggregates[0].configured_trials == 2
    assert aggregates[0].total == 1
    assert aggregates[0].skipped == 1
    assert aggregates[0].partial is False
    assert aggregates[0].verdict == "pass"
    assert aggregates[0].trials == [passed]
    assert aggregates[0].to_dict()["skipped"] == 1
    assert aggregate_trials([skipped]) == []


def test_run_summary_excludes_setup_and_teardown_from_all_metrics() -> None:
    summary = run_summary(
        {
            "trials": [
                _trial(status="pass", duration_ms=100, cost_usd=0.1),
                _trial(
                    status="error",
                    duration_ms=0,
                    cost_usd=0.2,
                    error_kind="setup",
                ),
                _trial(
                    status="error",
                    duration_ms=0,
                    cost_usd=0.3,
                    error_kind="teardown",
                ),
            ]
        }
    )

    assert summary["eligible_agent_trials"] == 1
    assert summary["pass_k_curve"] == [{"k": 1, "value": 1.0, "cohorts": 1}]
    assert summary["cost_latency"]["latency_mean_ms"] == 100
    assert summary["cost_latency"]["known_cost_usd"] == 0.1
    assert summary["cost_latency"]["cost_relevant_trials"] == 1


def test_run_summary_uses_internal_and_legacy_smoke_identity() -> None:
    legacy_id = _trial(status="pass", case_id="kensa_smoke", smoke=None)
    legacy_node = _trial(
        status="pass",
        case_id="readiness",
        group_id="tests/evals/test_kensa_smoke.py::test_kensa_smoke",
        smoke=None,
    )
    marked = _trial(status="pass", case_id="readiness", smoke=True)
    public_case_field = _trial(
        status="pass",
        case_id="domain",
        group_id="domain-group",
        smoke=False,
        case_smoke=True,
    )

    summary = run_summary({"trials": [legacy_id, legacy_node, marked, public_case_field]})

    assert summary["eligible_agent_trials"] == 1
    assert summary["pass_k_cohorts"] == [
        {
            "group_id": "domain-group",
            "case_id": "domain",
            "passed": 1,
            "total": 1,
        }
    ]
    assert is_smoke_trial(legacy_id)
    assert is_smoke_identity(case_id="", nodeid=legacy_node["group_id"])
    assert is_smoke_aggregate({"trials": [legacy_id]})
    assert is_smoke_aggregate({"group_id": legacy_node["group_id"]})
    assert not is_smoke_aggregate({"smoke": False, "case_id": "kensa_smoke"})


def test_smoke_identity_requires_exact_pytest_node() -> None:
    assert is_smoke_identity(
        case_id="",
        nodeid="tests/evals/test_kensa_smoke.py::test_kensa_smoke[readiness-trial1]",
    )
    assert not is_smoke_identity(
        case_id="refund",
        nodeid="tests/evals/test_kensa_smoke.py::test_kensa_smoke_refund[refund-trial1]",
    )
    assert not is_smoke_identity(
        case_id="refund",
        nodeid="tests/evals/test_refund.py::test_kensa_smoke[refund-trial1]",
    )


def test_terminal_reports_cohort_population_and_cost_coverage() -> None:
    class Terminal:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def write_line(self, line: str) -> None:
            self.lines.append(line)

    terminal = Terminal()
    partial = run_summary(
        {
            "trials": [
                _trial(status="pass", group_id="mode-a", cost_usd=0.1),
                _trial(
                    status="pass",
                    group_id="mode-a",
                    cost_usd=None,
                    cost_available=False,
                ),
                _trial(
                    status="pass",
                    group_id="mode-b",
                    cost_usd=None,
                    llm_turns=0,
                    cost_available=False,
                ),
            ]
        }
    )
    _write_scoring_summary(cast(Any, terminal), partial)

    assert "Reliability: pass^1 100.0% (2 cohorts) | pass^2 100.0% (1 cohort)" in terminal.lines
    assert "Cost: partial $0.1000 known | 1/2 fully priced trials" in terminal.lines

    _write_scoring_summary(
        cast(Any, terminal),
        run_summary({"trials": [_trial(status="pass", cost_usd=0.0, cost_available=True)]}),
    )
    assert "Cost: total $0.0000 | per pass $0.0000" in terminal.lines

    _write_scoring_summary(
        cast(Any, terminal),
        run_summary({"trials": [_trial(status="pass", cost_usd=None, cost_available=False)]}),
    )
    assert "Cost: n/a | 0/1 fully priced trials" in terminal.lines


def test_run_summary_empty_trials_returns_zero_metrics() -> None:
    expected = {
        "pass_k_curve": [],
        "pass_k_cohorts": [],
        "eligible_agent_trials": 0,
        "cost_latency": {
            "latency_p50_ms": 0.0,
            "latency_p95_ms": 0.0,
            "latency_mean_ms": 0.0,
            "total_cost_usd": None,
            "known_cost_usd": 0,
            "cost_per_pass_usd": None,
            "mean_llm_turns": 0.0,
            "cost_known_trials": 0,
            "cost_relevant_trials": 0,
            "cost_coverage": 0.0,
            "has_cost": False,
            "cost_complete": False,
            "cost_partial": False,
        },
    }
    assert run_summary({}) == expected
    assert run_summary({"trials": "invalid"}) == expected


def test_artifact_and_markdown_reports_include_run_summary(tmp_path: Path) -> None:
    result_path = tmp_path / "results" / "run.json"
    write_run_artifacts(
        run_id="run",
        trials=[
            TrialMetadata(
                nodeid="test_eval.py::test_agent[trial1-case-a]",
                group_id="test_eval.py::test_agent[case-a]",
                case_id="case-a",
                trial_index=1,
                configured_trials=1,
                status="pass",
                case={"id": "case-a"},
                duration_ms=125,
                trace={
                    "cost_usd": 0.02,
                    "cost_available": True,
                    "llm_turns": 3,
                },
            )
        ],
        result_path=result_path,
        artifact_dir=tmp_path,
    )

    payload = json.loads(result_path.read_text())
    assert payload["trials"][0]["smoke"] is False
    assert payload["aggregates"][0]["smoke"] is False
    assert payload["summary"]["pass_k_curve"] == [{"k": 1, "value": 1.0, "cohorts": 1}]
    assert payload["summary"]["cost_latency"]["total_cost_usd"] == 0.02

    markdown_path = tmp_path / "report.md"
    cli._write_markdown_report(result_path, markdown_path)
    markdown = markdown_path.read_text()
    assert "| k | pass^k | Cohorts |" in markdown
    assert "| 1 | 100.0% | 1 |" in markdown
    assert "Latency p50: 125ms" in markdown
    assert "Total cost: $0.0200" in markdown
    assert "Cost coverage: 1/1 fully priced trials" in markdown


def test_artifact_marks_legacy_smoke_and_markdown_reports_partial_cost(
    tmp_path: Path,
) -> None:
    result_path = tmp_path / "results" / "run.json"
    write_run_artifacts(
        run_id="run",
        trials=[
            TrialMetadata(
                nodeid="tests/evals/test_kensa_smoke.py::test_kensa_smoke[trial1]",
                group_id="tests/evals/test_kensa_smoke.py::test_kensa_smoke",
                case_id="kensa_smoke",
                trial_index=1,
                configured_trials=1,
                status="pass",
            ),
            TrialMetadata(
                nodeid="test_eval.py::test_agent[trial1-case-a]",
                group_id="test_eval.py::test_agent[case-a]",
                case_id="case-a",
                trial_index=1,
                configured_trials=2,
                status="pass",
                trace={
                    "cost_usd": 0.02,
                    "cost_available": True,
                    "llm_turns": 1,
                },
            ),
            TrialMetadata(
                nodeid="test_eval.py::test_agent[trial2-case-a]",
                group_id="test_eval.py::test_agent[case-a]",
                case_id="case-a",
                trial_index=2,
                configured_trials=2,
                status="fail",
                trace={
                    "cost_usd": None,
                    "cost_available": False,
                    "llm_turns": 1,
                },
            ),
        ],
        result_path=result_path,
        artifact_dir=tmp_path,
    )

    payload = json.loads(result_path.read_text())
    assert payload["trials"][0]["smoke"] is True
    smoke_aggregate = next(
        aggregate for aggregate in payload["aggregates"] if aggregate["case_id"] == "kensa_smoke"
    )
    assert smoke_aggregate["smoke"] is True
    assert payload["summary"]["eligible_agent_trials"] == 2
    assert payload["summary"]["cost_latency"]["total_cost_usd"] is None

    markdown_path = tmp_path / "report.md"
    cli._write_markdown_report(result_path, markdown_path)
    markdown = markdown_path.read_text()
    assert "Total cost: partial: $0.0200 known" in markdown
    assert "Cost coverage: 1/2 fully priced trials" in markdown
