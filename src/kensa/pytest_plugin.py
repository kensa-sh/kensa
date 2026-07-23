"""Pytest plugin for Kensa agent evals."""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from kensa.artifacts import (
    KensaAggregate,
    aggregate_trials,
    trial_from_dict,
    trial_sort_key,
    upsert_trial,
    write_run_artifacts,
)
from kensa.case import KensaCase
from kensa.runtime import (
    ActiveOperation,
    KensaTrace,
    KensaTrial,
    KensaTrialRuntime,
    TrialMetadata,
    ensure_tracing,
    reset_current_runtime,
    set_current_runtime,
)
from kensa.scoring import run_summary
from kensa.watchdog import (
    DEFAULT_JUDGE_TIMEOUT_S,
    ActiveTrial,
    read_control,
    validate_timeout_s,
    worker_control_path,
    write_control,
)

PRIVATE_TRIAL = "_kensa_trial"
_PROVISIONAL_STATUS = "provisional"
_TRIAL_RE = re.compile(r"-?trial\d+-?|trial\d+-?")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_TRIAL_METADATA_REPORT_KEY = "_kensa_trial_metadata"
_WORKER_CONTROL_PATH_KEY = "_kensa_control_path"
_EACH_DIST_ERROR = (
    "pytest --dist=each is incompatible with Kensa trials because it runs every trial "
    "on every worker. Use load or worksteal distribution."
)


class KensaSessionState:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        workerinput = getattr(config, "workerinput", {})
        raw_control_path = (
            workerinput.get(_WORKER_CONTROL_PATH_KEY) if isinstance(workerinput, dict) else None
        )
        if raw_control_path is None:
            getoption = getattr(config, "getoption", None)
            raw_control_path = getoption("--kensa-control-path") if callable(getoption) else None
        self.control_path = Path(raw_control_path) if raw_control_path else None
        self.control = read_control(self.control_path) if self.control_path else None
        configured_run_id = self.control.run_id if self.control is not None else None
        if configured_run_id is not None and _RUN_ID_RE.fullmatch(configured_run_id) is None:
            raise pytest.UsageError("Kensa control file contains an invalid Kensa run ID")
        self.run_id = configured_run_id or uuid4().hex
        self.trials: list[TrialMetadata] = []
        self.aggregates: list[KensaAggregate] = []
        self.complete = True
        self.interruption: dict[str, Any] | None = None

    @property
    def artifact_dir(self) -> Path:
        if self.control is not None:
            return self.control.artifact_dir
        getoption = getattr(self.config, "getoption", None)
        raw = getoption("--kensa-artifact-dir") if callable(getoption) else None
        return Path(raw) if raw else Path.cwd() / ".kensa"

    @property
    def result_path(self) -> Path:
        if self.control is not None:
            return self.control.result_path
        return self.artifact_dir / "results" / f"{self.run_id}.json"

    @property
    def write_artifacts(self) -> bool:
        getoption = getattr(self.config, "getoption", None)
        requested = bool(getoption("--kensa-write-artifacts")) if callable(getoption) else False
        return not _is_xdist_worker(self.config) and (self.control is not None or requested)

    def set_active_trial(self, active_trial: ActiveTrial | None) -> None:
        if self.control_path is None or self.control is None:
            return
        self.control = replace(
            self.control,
            active_trial=active_trial,
            trial_snapshot=None if active_trial is not None else self.control.trial_snapshot,
        )
        write_control(self.control_path, self.control)

    def set_trial_snapshot(self, snapshot: TrialMetadata) -> None:
        if self.control_path is None or self.control is None:
            return
        self.control = replace(self.control, trial_snapshot=snapshot)
        write_control(self.control_path, self.control)

    def set_active_operation(
        self,
        nodeid: str,
        operation: ActiveOperation | None,
    ) -> None:
        if self.control_path is None or self.control is None:
            return
        active_trial = self.control.active_trial
        if active_trial is None or active_trial.nodeid != nodeid:
            return
        self.control = replace(
            self.control,
            active_trial=replace(active_trial, active_operation=operation),
        )
        write_control(self.control_path, self.control)

    def mark_incomplete(self, kind: str, message: str, **details: Any) -> None:
        self.complete = False
        self.interruption = {"kind": kind, "message": message, **details}


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
    group.addoption(
        "--kensa-control-path",
        default=None,
        help="Internal control path supplied by kensa eval.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "kensa(trials=1, timeout_s=None): mark a Kensa agent eval",
    )
    _state(config)
    ensure_tracing()


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    state = _state(session.config)
    expected_workers = state.control.expected_workers if state.control is not None else None
    if expected_workers is None or _is_xdist_worker(session.config):
        return
    _validate_worker_configuration(session.config, expected_workers)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:
    state = _state(node.config)
    if state.control_path is None or state.control is None:
        return
    if not node.gateway.spec.popen:
        return
    worker_id = str(node.workerinput["workerid"])
    path = worker_control_path(state.control_path, worker_id)
    write_control(
        path,
        replace(
            state.control,
            active_trial=None,
            expected_workers=None,
            trial_snapshot=None,
        ),
    )
    node.workerinput[_WORKER_CONTROL_PATH_KEY] = str(path)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: object | None) -> None:
    raw_path = node.workerinput.get(_WORKER_CONTROL_PATH_KEY)
    if isinstance(raw_path, str):
        Path(raw_path).unlink(missing_ok=True)
    if error is not None:
        _state(node.config).mark_incomplete("worker_crash", str(error))


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
    timeout_s = _marker_timeout(marker, metafunc.config)
    if PRIVATE_TRIAL not in metafunc.fixturenames:
        metafunc.fixturenames.append(PRIVATE_TRIAL)
    values = [KensaTrial(i, trials, timeout_s=timeout_s) for i in range(1, trials + 1)]
    metafunc.parametrize(PRIVATE_TRIAL, values, ids=[v.id for v in values], indirect=True)


def _validate_worker_configuration(config: pytest.Config, expected_workers: int) -> None:
    numprocesses = config.getoption("numprocesses")
    tx = config.getoption("tx")
    px = config.getoption("px")
    if px or any(spec != "popen" for spec in tx):
        raise pytest.UsageError(
            "Kensa eval supports local pytest workers only; remove configured --tx and --px "
            "gateways."
        )
    if config.getoption("dist") == "each":
        raise pytest.UsageError(_EACH_DIST_ERROR)
    resolved_workers = len(tx)
    if expected_workers == 1:
        matches = numprocesses in (None, 0) and resolved_workers == 0
    else:
        matches = numprocesses == expected_workers and resolved_workers == expected_workers
    if not matches:
        raise pytest.UsageError(
            f"Kensa eval expected {expected_workers} local pytest worker"
            f"{'s' if expected_workers != 1 else ''}, but pytest resolved "
            f"{resolved_workers}. Remove conflicting pytest addopts, PYTEST_ADDOPTS, and "
            "xdist limits."
        )


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


def _marker_timeout(marker: pytest.Mark, config: pytest.Config) -> float | None:
    explicit = "timeout_s" in marker.kwargs
    if explicit:
        try:
            timeout_s = validate_timeout_s(marker.kwargs["timeout_s"])
        except ValueError as exc:
            raise pytest.UsageError(
                "@pytest.mark.kensa(timeout_s=SECONDS) requires a positive finite number"
            ) from exc
        if not config.getoption("--kensa-control-path"):
            raise pytest.UsageError(
                "@pytest.mark.kensa(timeout_s=...) requires kensa eval for hard containment"
            )
        return timeout_s
    state = _state(config)
    return state.control.default_timeout_s if state.control is not None else None


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
    state = _state(item.config)
    runtime = KensaTrialRuntime(
        trial=trial,
        nodeid=item.nodeid,
        group_id=_group_id(item),
        case_id=_case_id(item),
        no_judge=bool(item.config.getoption("--kensa-no-judge")),
        judge_timeout_s=(
            state.control.judge_timeout_s if state.control is not None else DEFAULT_JUDGE_TIMEOUT_S
        ),
        operation_callback=lambda operation: state.set_active_operation(item.nodeid, operation),
        snapshot_callback=lambda completed: _record_trial_snapshot(item.config, completed),
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
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Any:
    del nextitem
    runtime = _runtime_for_item(item)
    if runtime is None:
        yield
        return
    token = set_current_runtime(runtime)
    state = _state(item.config)
    timeout_s = runtime.trial.timeout_s
    watchdog_active = False
    if state.control is not None and timeout_s is not None:
        watchdog_active = True
        state.set_active_trial(
            ActiveTrial(
                nodeid=runtime.nodeid,
                group_id=runtime.group_id,
                case_id=runtime.case_id,
                trial_index=runtime.trial.trial_index,
                configured_trials=runtime.trial.configured_trials,
                timeout_s=timeout_s,
                started_monotonic_ns=time.monotonic_ns(),
            )
        )
    try:
        yield
    finally:
        if watchdog_active:
            state.set_active_trial(None)
        reset_current_runtime(token)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Any:
    runtime = _runtime_for_item(item)
    if runtime is None:
        yield
        return

    start = time.monotonic()
    outcome = yield
    duration_ms = (time.monotonic() - start) * 1000

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
    runtime = _runtime_for_item(item)
    if runtime is None:
        return
    if report.when == "setup" and getattr(report, "skipped", False):
        return
    if report.when in {"setup", "teardown"} and report.failed:
        existing = _trial_metadata(item.config, item.nodeid)
        if report.when != "setup" or existing is None or existing.status == _PROVISIONAL_STATUS:
            _record_trial(
                item.config,
                runtime.metadata(
                    status="error",
                    duration_ms=0.0,
                    error=(
                        str(call.excinfo.value) if call.excinfo else f"pytest {report.when} failed"
                    ),
                    error_kind=report.when,
                ),
            )
    if report.when in {"call", "teardown"} or (report.when == "setup" and report.failed):
        metadata = _trial_metadata(item.config, item.nodeid)
        if metadata is not None:
            report.__dict__[_TRIAL_METADATA_REPORT_KEY] = metadata.to_dict()


def _record_trial(config: pytest.Config, metadata: TrialMetadata) -> None:
    state = _state(config)
    upsert_trial(state.trials, metadata)
    if _is_xdist_worker(config):
        state.set_trial_snapshot(metadata)
    if state.write_artifacts:
        _write_artifacts(state)


def _record_trial_snapshot(config: pytest.Config, runtime: KensaTrialRuntime) -> None:
    state = _state(config)
    existing = next((trial for trial in state.trials if trial.nodeid == runtime.nodeid), None)
    snapshot = runtime.metadata(
        status=_PROVISIONAL_STATUS,
        duration_ms=runtime.trace.duration_ms,
    )
    if existing is not None and existing.status != _PROVISIONAL_STATUS:
        snapshot = replace(
            existing,
            case=snapshot.case,
            output=snapshot.output,
            trace=snapshot.trace,
            judges=snapshot.judges,
        )
    _record_trial(config, snapshot)


def _trial_metadata(config: pytest.Config, nodeid: str) -> TrialMetadata | None:
    return next((trial for trial in _state(config).trials if trial.nodeid == nodeid), None)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    node = getattr(report, "node", None)
    config = getattr(node, "config", None)
    if config is None:
        return
    state = _state(config)
    payload = getattr(report, _TRIAL_METADATA_REPORT_KEY, None)
    if isinstance(payload, dict):
        metadata = trial_from_dict(payload)
        _record_trial(config, metadata)
    elif report.when == "???":
        state.mark_incomplete(
            "worker_crash",
            str(report.longrepr),
            nodeid=report.nodeid,
        )
        if state.write_artifacts:
            _write_artifacts(state)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    if _is_xdist_worker(session.config):
        return
    state = _state(session.config)
    stopped = session.shouldstop or session.shouldfail
    if stopped:
        state.mark_incomplete("pytest_stopped", str(stopped))
    elif exitstatus in {
        pytest.ExitCode.INTERRUPTED,
        pytest.ExitCode.INTERNAL_ERROR,
        pytest.ExitCode.USAGE_ERROR,
    }:
        state.mark_incomplete("pytest_error", f"pytest exited with status {int(exitstatus)}")
    state.trials.sort(key=trial_sort_key)
    state.aggregates = aggregate_trials(state.trials)
    if state.write_artifacts and (state.trials or state.control is not None):
        _write_artifacts(state)
    if any(aggregate.verdict != "pass" for aggregate in state.aggregates):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _write_artifacts(state: KensaSessionState) -> None:
    state.aggregates = write_run_artifacts(
        run_id=state.run_id,
        trials=state.trials,
        result_path=state.result_path,
        artifact_dir=state.artifact_dir,
        complete=state.complete,
        interruption=state.interruption,
    )


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int | pytest.ExitCode,
    config: pytest.Config,
) -> None:
    del exitstatus
    if _is_xdist_worker(config):
        return
    state = _state(config)
    if not state.trials:
        return
    terminalreporter.write_sep("=", "Kensa evaluation complete")
    summary = run_summary({"trials": [trial.to_dict() for trial in state.trials]})
    if config.getoption("--kensa-report") == "json":
        terminalreporter.write_line(
            json.dumps(
                {
                    "run_id": state.run_id,
                    "aggregates": [aggregate.to_dict() for aggregate in state.aggregates],
                    "summary": summary,
                },
                indent=2,
            )
        )
        return
    results = [
        f"{_status_marker(aggregate.verdict)} {aggregate.verdict}" for aggregate in state.aggregates
    ]
    passed = sum(1 for aggregate in state.aggregates if aggregate.verdict == "pass")
    terminalreporter.write_line(f"{passed}/{len(state.aggregates)} aggregate case(s) passed")
    _write_scoring_summary(terminalreporter, summary)
    terminalreporter.write_line("")
    case_counts = Counter(aggregate.case_id for aggregate in state.aggregates)
    case_labels = [
        aggregate.group_id if case_counts[aggregate.case_id] > 1 else aggregate.case_id
        for aggregate in state.aggregates
    ]
    result_width = max(len("Result"), *(len(result) for result in results)) + 2
    case_width = max(len("Case"), *(len(case_label) for case_label in case_labels)) + 2
    terminalreporter.write_line(f"{'Result':<{result_width}}{'Case':<{case_width}}Trials")
    for aggregate, result, case_label in zip(state.aggregates, results, case_labels, strict=True):
        trials = "  ".join(
            f"{_status_marker(trial.status)} T{trial.trial_index}" for trial in aggregate.trials
        )
        terminalreporter.write_line(f"{result:<{result_width}}{case_label:<{case_width}}{trials}")


def _write_scoring_summary(
    terminalreporter: pytest.TerminalReporter,
    summary: dict[str, Any],
) -> None:
    curve = summary["pass_k_curve"]
    reliability = (
        " | ".join(
            f"pass^{point['k']} "
            f"{float(point['value']):.1%} ({_cohort_count(int(point['cohorts']))})"
            for point in curve
        )
        or "n/a"
    )
    performance = summary["cost_latency"]
    terminalreporter.write_line(f"Reliability: {reliability}")
    terminalreporter.write_line(
        "Latency: "
        f"p50 {_format_duration(float(performance['latency_p50_ms']))} | "
        f"p95 {_format_duration(float(performance['latency_p95_ms']))} | "
        f"mean {_format_duration(float(performance['latency_mean_ms']))}"
    )
    terminalreporter.write_line(f"Mean LLM turns: {float(performance['mean_llm_turns']):.1f}")
    known_trials = int(performance["cost_known_trials"])
    relevant_trials = int(performance["cost_relevant_trials"])
    if performance["cost_complete"]:
        terminalreporter.write_line(
            f"Cost: total ${float(performance['total_cost_usd']):.4f} | "
            f"per pass {_format_cost(performance['cost_per_pass_usd'])}"
        )
    elif performance["cost_partial"]:
        terminalreporter.write_line(
            f"Cost: partial ${float(performance['known_cost_usd']):.4f} known | "
            f"{known_trials}/{relevant_trials} fully priced trials"
        )
    elif relevant_trials:
        terminalreporter.write_line(
            f"Cost: n/a | {known_trials}/{relevant_trials} fully priced trials"
        )
    else:
        terminalreporter.write_line("Cost: n/a")


def _cohort_count(count: int) -> str:
    return f"{count} {'cohort' if count == 1 else 'cohorts'}"


def _format_duration(milliseconds: float) -> str:
    return f"{milliseconds:.0f}ms" if milliseconds < 1000 else f"{milliseconds / 1000:.1f}s"


def _format_cost(value: Any) -> str:
    return "n/a" if value is None else f"${float(value):.4f}"


def _status_marker(status: str) -> str:
    return {"pass": "✓", "fail": "✗"}.get(status, "!")


def _is_xdist_worker(config: pytest.Config) -> bool:
    return hasattr(config, "workerinput")


__all__ = ["PRIVATE_TRIAL"]
