"""Pytest plugin for Kensa agent evals."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kensa.case import KensaCase
from kensa.runtime import (
    KensaTrace,
    KensaTrial,
    KensaTrialRuntime,
    TrialMetadata,
    ensure_tracing,
    reset_current_runtime,
    set_current_runtime,
)

PRIVATE_TRIAL = "_kensa_trial"
_TRIAL_RE = re.compile(r"-?trial\d+-?|trial\d+-?")


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "case_id": self.case_id,
            "configured_trials": self.configured_trials,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
            "partial": self.partial,
            "verdict": self.verdict,
            "trials": [trial.to_dict() for trial in self.trials],
        }


class KensaSessionState:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        self.trials: list[TrialMetadata] = []
        self.aggregates: list[KensaAggregate] = []

    @property
    def artifact_dir(self) -> Path:
        raw = self.config.getoption("--kensa-artifact-dir")
        return Path(raw) if raw else Path.cwd() / ".kensa"

    @property
    def write_artifacts(self) -> bool:
        return bool(self.config.getoption("--kensa-write-artifacts"))


def _state(config: pytest.Config) -> KensaSessionState:
    state = getattr(config, "_kensa_state", None)
    if isinstance(state, KensaSessionState):
        return state
    state = KensaSessionState(config)
    config.__dict__["_kensa_state"] = state
    return state


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("kensa")
    group.addoption("--kensa-no-judge", action="store_true", help="Disable judge calls.")
    group.addoption(
        "--kensa-report",
        choices=("term", "json"),
        default="term",
        help="Kensa terminal summary format.",
    )
    group.addoption(
        "--kensa-write-artifacts",
        action="store_true",
        help="Write Kensa JSON run artifacts.",
    )
    group.addoption(
        "--kensa-artifact-dir",
        default=None,
        help="Directory for Kensa artifacts. Defaults to .kensa.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "kensa(trials=1): mark a Kensa agent eval",
    )
    _state(config)
    ensure_tracing()


def pytest_make_parametrize_id(config: pytest.Config, val: Any, argname: str) -> str | None:
    del config, argname
    if isinstance(val, KensaCase):
        return val.id
    if isinstance(val, KensaTrial):
        return val.id
    return None


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    definition = metafunc.definition
    marker = definition.get_closest_marker("kensa") if definition is not None else None
    if marker is None:
        return
    trials = _marker_trials(marker)
    if PRIVATE_TRIAL not in metafunc.fixturenames:
        metafunc.fixturenames.append(PRIVATE_TRIAL)
    values = [KensaTrial(i, trials) for i in range(1, trials + 1)]
    metafunc.parametrize(PRIVATE_TRIAL, values, ids=[v.id for v in values], indirect=True)


def _marker_trials(marker: pytest.Mark) -> int:
    raw = marker.kwargs.get("trials", 1)
    if marker.args:
        if len(marker.args) == 1 and isinstance(marker.args[0], int):
            raw = marker.args[0]
        else:
            raise pytest.UsageError("@pytest.mark.kensa only accepts trials=N")
    if not isinstance(raw, int) or raw < 1:
        raise pytest.UsageError("@pytest.mark.kensa(trials=N) requires N >= 1")
    return raw


@pytest.fixture(name=PRIVATE_TRIAL)
def _kensa_trial_fixture(request: pytest.FixtureRequest) -> KensaTrial:
    param = getattr(request, "param", None)
    if not isinstance(param, KensaTrial):
        return KensaTrial(1, 1)
    return param


@pytest.fixture
def kensa_trace(request: pytest.FixtureRequest) -> KensaTrace:
    runtime = _runtime_for_item(request.node)
    if runtime is None:
        return KensaTrace()
    return runtime.trace


def _runtime_for_item(item: pytest.Item) -> KensaTrialRuntime | None:
    existing = getattr(item, "_kensa_runtime", None)
    if isinstance(existing, KensaTrialRuntime):
        return existing
    trial = _trial_from_item(item)
    if trial is None:
        return None
    marker = item.get_closest_marker("kensa")
    if marker is None:
        return None
    runtime = KensaTrialRuntime(
        trial=trial,
        nodeid=item.nodeid,
        group_id=_group_id(item),
        case_id=_case_id(item),
        no_judge=bool(item.config.getoption("--kensa-no-judge")),
    )
    item.__dict__["_kensa_runtime"] = runtime
    return runtime


def _trial_from_item(item: pytest.Item) -> KensaTrial | None:
    callspec = getattr(item, "callspec", None)
    params = getattr(callspec, "params", {}) if callspec is not None else {}
    trial = params.get(PRIVATE_TRIAL) if isinstance(params, dict) else None
    return trial if isinstance(trial, KensaTrial) else None


def _case_id(item: pytest.Item) -> str:
    callspec = getattr(item, "callspec", None)
    params = getattr(callspec, "params", {}) if callspec is not None else {}
    if isinstance(params, dict):
        for value in params.values():
            if isinstance(value, KensaCase):
                return value.id
    return "default"


def _group_id(item: pytest.Item) -> str:
    normalized = _TRIAL_RE.sub("", item.nodeid)
    return normalized.replace("[]", "")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Any:
    runtime = _runtime_for_item(item)
    if runtime is None:
        yield
        return

    token = set_current_runtime(runtime)
    start = time.monotonic()
    outcome = yield
    duration_ms = (time.monotonic() - start) * 1000
    reset_current_runtime(token)

    excinfo = outcome.excinfo
    if excinfo is None:
        _record_trial(item.config, runtime.metadata(status="pass", duration_ms=duration_ms))
        return

    exc = excinfo[1]
    if isinstance(exc, AssertionError):
        status = "fail"
        error_kind = "assertion"
    else:
        status = "error"
        error_kind = "exception"
    _record_trial(
        item.config,
        runtime.metadata(
            status=status,
            duration_ms=duration_ms,
            error=str(exc),
            error_kind=error_kind,
        ),
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Any:
    outcome = yield
    report = outcome.get_result()
    if report.when not in {"setup", "teardown"} or not report.failed:
        return
    runtime = _runtime_for_item(item)
    if runtime is None:
        return
    if report.when == "setup" and any(t.nodeid == item.nodeid for t in _state(item.config).trials):
        return
    _record_trial(
        item.config,
        runtime.metadata(
            status="error",
            duration_ms=0.0,
            error=str(call.excinfo.value) if call.excinfo else f"pytest {report.when} failed",
            error_kind=report.when,
        ),
    )


def _record_trial(config: pytest.Config, metadata: TrialMetadata) -> None:
    trials = _state(config).trials
    for index, existing in enumerate(trials):
        if existing.nodeid == metadata.nodeid:
            trials[index] = metadata
            return
    trials.append(metadata)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    state = _state(session.config)
    state.aggregates = _aggregate(state.trials)
    if state.write_artifacts and state.trials:
        _write_artifacts(state)
    if any(aggregate.verdict != "pass" for aggregate in state.aggregates):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _aggregate(trials: list[TrialMetadata]) -> list[KensaAggregate]:
    groups: dict[str, list[TrialMetadata]] = {}
    for trial in trials:
        groups.setdefault(trial.group_id, []).append(trial)
    aggregates: list[KensaAggregate] = []
    for group_id, group_trials in sorted(groups.items()):
        ordered = sorted(group_trials, key=lambda t: t.trial_index)
        total = len(ordered)
        passed = sum(1 for t in ordered if t.status == "pass")
        errored = sum(1 for t in ordered if t.status == "error")
        failed = sum(1 for t in ordered if t.status == "fail")
        configured = ordered[0].configured_trials if ordered else 0
        partial = total < configured
        if partial:
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
                case_id=ordered[0].case_id if ordered else "default",
                configured_trials=configured,
                total=total,
                passed=passed,
                failed=failed,
                errored=errored,
                partial=partial,
                verdict=verdict,
                trials=ordered,
            )
        )
    return aggregates


def _write_artifacts(state: KensaSessionState) -> None:
    result_dir = state.artifact_dir / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": state.run_id,
        "trials": [trial.to_dict() for trial in state.trials],
        "aggregates": [aggregate.to_dict() for aggregate in state.aggregates],
    }
    (result_dir / f"{state.run_id}.json").write_text(json.dumps(payload, indent=2))
    _write_trace_artifact(state)


def _write_trace_artifact(state: KensaSessionState) -> None:
    trace_dir = state.artifact_dir / "traces" / "runs" / state.run_id
    trace_dir.mkdir(parents=True, exist_ok=True)
    rows = [_trial_trace_record(state.run_id, trial) for trial in state.trials if trial.case]
    if not rows:
        return
    output = trace_dir / "trials.jsonl"
    output.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n")


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
        "duration_ms": trial.duration_ms,
        "spans": spans,
    }


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int | pytest.ExitCode,
    config: pytest.Config,
) -> None:
    del exitstatus
    state = _state(config)
    if not state.trials:
        return
    terminalreporter.write_sep("=", "Kensa summary")
    if config.getoption("--kensa-report") == "json":
        terminalreporter.write_line(
            json.dumps(
                {
                    "run_id": state.run_id,
                    "aggregates": [aggregate.to_dict() for aggregate in state.aggregates],
                },
                indent=2,
            )
        )
        return
    passed = sum(1 for aggregate in state.aggregates if aggregate.verdict == "pass")
    terminalreporter.write_line(f"{passed}/{len(state.aggregates)} aggregate case(s) passed")
    for aggregate in state.aggregates:
        label = aggregate.verdict.upper()
        terminalreporter.write_line(
            f"{label} {aggregate.group_id}: {aggregate.passed}/{aggregate.total} passed, "
            f"{aggregate.failed} failed, {aggregate.errored} errored"
        )


__all__ = ["PRIVATE_TRIAL"]
