from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from kensa import cli
from kensa._smoke import is_smoke_aggregate, is_smoke_identity, is_smoke_trial
from kensa.artifacts import write_run_artifacts
from kensa.engine import EngineClient
from kensa.errors import TrialFailure
from kensa.pytest_plugin import _write_scoring_summary
from kensa.runtime import TrialMetadata


def _trial(
    *,
    nodeid: str,
    group_id: str,
    case_id: str,
    trial_index: int = 1,
    configured_trials: int = 1,
    status: str = "pass",
    cost_usd: float | None = 0.01,
    cost_available: bool = True,
    llm_turns: Any = 2,
    smoke: bool = False,
    failure_category: str = "agent",
) -> TrialMetadata:
    failure = (
        TrialFailure(
            category=cast(Any, failure_category),
            kind="assertion" if status == "fail" else "execution",
            message="trial failed",
        )
        if status in {"fail", "error"}
        else None
    )
    return TrialMetadata(
        nodeid=nodeid,
        group_id=group_id,
        case_id=case_id,
        trial_index=trial_index,
        configured_trials=configured_trials,
        status=status,
        case={"id": case_id},
        failure=failure,
        duration_ms=125.0,
        trace={
            "cost_usd": cost_usd,
            "cost_available": cost_available,
            "llm_turns": llm_turns,
        },
        smoke=smoke,
    )


def _core_result(
    *,
    run_id: str,
    trials: list[TrialMetadata],
    complete: bool = True,
) -> dict[str, Any]:
    with EngineClient() as engine:
        return engine.build_run(
            run_id=run_id,
            complete=complete,
            interruption=None,
            trials=[trial.to_dict() for trial in trials],
        )


def _write_result(path: Path, trials: list[TrialMetadata]) -> None:
    core_result = _core_result(run_id="run", trials=trials)
    write_run_artifacts(
        run_id="run",
        trials=trials,
        result_path=path,
        artifact_dir=path.parent.parent,
        core_result=core_result,
    )


def test_core_summary_rejects_boolean_llm_turns() -> None:
    trial = _trial(
        nodeid="node",
        group_id="group",
        case_id="case",
        llm_turns=True,
    )

    result = _core_result(run_id="run", trials=[trial])

    assert result["summary"]["cost_latency"]["mean_llm_turns"] == 0.0


def test_terminal_formats_core_summary() -> None:
    class Terminal:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def write_line(self, line: str) -> None:
            self.lines.append(line)

    trials = [
        _trial(
            nodeid="mode-a-1",
            group_id="mode-a",
            case_id="case-a",
            trial_index=1,
            configured_trials=2,
            cost_usd=0.1,
        ),
        _trial(
            nodeid="mode-a-2",
            group_id="mode-a",
            case_id="case-a",
            trial_index=2,
            configured_trials=2,
            cost_usd=None,
            cost_available=False,
        ),
        _trial(
            nodeid="excluded",
            group_id="excluded",
            case_id="excluded",
            status="error",
            cost_usd=None,
            cost_available=False,
            failure_category="simulator",
        ),
    ]
    summary = _core_result(run_id="run", trials=trials)["summary"]
    terminal = Terminal()

    _write_scoring_summary(cast(Any, terminal), summary)

    assert "Eligible agent trials: 2" in terminal.lines
    assert "Excluded errors: simulator 1" in terminal.lines
    assert "Reliability: pass^1 100.0% (1 cohort) | pass^2 100.0% (1 cohort)" in terminal.lines
    assert "Cost: partial $0.1000 known | 1/2 fully priced trials" in terminal.lines


def test_terminal_formats_complete_and_unavailable_costs() -> None:
    class Terminal:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def write_line(self, line: str) -> None:
            self.lines.append(line)

    terminal = Terminal()
    priced_failure = _trial(
        nodeid="failed",
        group_id="failed",
        case_id="failed",
        status="fail",
        cost_usd=0.1,
    )
    _write_scoring_summary(
        cast(Any, terminal),
        _core_result(run_id="priced", trials=[priced_failure])["summary"],
    )
    unavailable = _trial(
        nodeid="unavailable",
        group_id="unavailable",
        case_id="unavailable",
        cost_usd=None,
        cost_available=False,
        llm_turns=1,
    )
    _write_scoring_summary(
        cast(Any, terminal),
        _core_result(run_id="unavailable", trials=[unavailable])["summary"],
    )

    assert "Cost: total $0.1000 | per pass n/a" in terminal.lines
    assert "Cost: n/a | 0/1 fully priced trials" in terminal.lines


def test_smoke_identity_contract() -> None:
    legacy_trial = {
        "case_id": "kensa_smoke",
        "group_id": "tests/evals/test_kensa_smoke.py::test_kensa_smoke",
    }

    assert is_smoke_trial(legacy_trial)
    assert is_smoke_aggregate({"trials": [legacy_trial]})
    assert is_smoke_aggregate({"group_id": legacy_trial["group_id"]})
    assert not is_smoke_aggregate({"smoke": False, "case_id": "kensa_smoke"})
    assert is_smoke_identity(
        case_id="",
        nodeid="tests/evals/test_kensa_smoke.py::test_kensa_smoke[readiness-trial1]",
    )
    assert not is_smoke_identity(
        case_id="refund",
        nodeid="tests/evals/test_kensa_smoke.py::test_kensa_smoke_refund[refund-trial1]",
    )


def test_artifact_and_markdown_use_core_summary(tmp_path: Path) -> None:
    result_path = tmp_path / "results" / "run.json"
    trials = [
        _trial(
            nodeid="case-a",
            group_id="case-a",
            case_id="case-a",
            cost_usd=0.02,
            llm_turns=3,
        ),
        _trial(
            nodeid="case-b",
            group_id="case-b",
            case_id="case-b",
            status="error",
            cost_usd=None,
            cost_available=False,
            failure_category="simulator",
        ),
    ]
    _write_result(result_path, trials)

    payload = json.loads(result_path.read_text())
    assert payload["summary"]["pass_k_curve"] == [{"k": 1, "value": 1.0, "cohorts": 1}]
    assert payload["summary"]["cost_latency"]["total_cost_usd"] == 0.02

    markdown_path = tmp_path / "report.md"
    cli._write_markdown_report(result_path, markdown_path)
    markdown = markdown_path.read_text()
    assert "| 1 | 100.0% | 1 |" in markdown
    assert "Total cost: $0.0200" in markdown
    assert "Cost coverage: 1/1 fully priced trials" in markdown
    assert "Excluded errors: simulator: 1" in markdown


def test_artifact_marks_smoke_and_reports_partial_cost(tmp_path: Path) -> None:
    result_path = tmp_path / "results" / "run.json"
    trials = [
        _trial(
            nodeid="smoke",
            group_id="smoke",
            case_id="kensa_smoke",
            smoke=True,
        ),
        _trial(
            nodeid="case-a-1",
            group_id="case-a",
            case_id="case-a",
            trial_index=1,
            configured_trials=2,
            cost_usd=0.02,
        ),
        _trial(
            nodeid="case-a-2",
            group_id="case-a",
            case_id="case-a",
            trial_index=2,
            configured_trials=2,
            status="fail",
            cost_usd=None,
            cost_available=False,
        ),
    ]
    _write_result(result_path, trials)

    payload = json.loads(result_path.read_text())
    smoke_trial = next(trial for trial in payload["trials"] if trial["case_id"] == "kensa_smoke")
    assert smoke_trial["smoke"] is True
    assert payload["summary"]["eligible_agent_trials"] == 2
    assert payload["summary"]["cost_latency"]["total_cost_usd"] is None

    markdown_path = tmp_path / "report.md"
    cli._write_markdown_report(result_path, markdown_path)
    markdown = markdown_path.read_text()
    assert "Total cost: partial: $0.0200 known" in markdown
    assert "Cost coverage: 1/2 fully priced trials" in markdown
