"""Pytest plugin for Kensa agent evals."""

from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kensa.artifacts import KensaAggregate, aggregate_trials, upsert_trial, write_run_artifacts
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
from kensa.watchdog import (
    DEFAULT_JUDGE_TIMEOUT_S,
    DISTRIBUTED_PYTEST_UNSUPPORTED,
    ActiveTrial,
    read_control,
    validate_timeout_s,
    write_control,
)

PRIVATE_TRIAL = "_kensa_trial"
_PROVISIONAL_STATUS = "provisional"
_TRIAL_RE = re.compile(r"-?trial\d+-?|trial\d+-?")


class KensaSessionState:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        raw_control_path = config.getoption("--kensa-control-path")
        self.control_path = Path(raw_control_path) if raw_control_path else None
        self.control = read_control(self.control_path) if self.control_path else None
        self.run_id = (
            self.control.run_id
            if self.control is not None
            else datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        )
        self.trials: list[TrialMetadata] = []
        self.aggregates: list[KensaAggregate] = []

    @property
    def artifact_dir(self) -> Path:
        if self.control is not None:
            return self.control.artifact_dir
        raw = self.config.getoption("--kensa-artifact-dir")
        return Path(raw) if raw else Path.cwd() / ".kensa"

    @property
    def result_path(self) -> Path:
        if self.control is not None:
            return self.control.result_path
        return self.artifact_dir / "results" / f"{self.run_id}.json"

    @property
    def write_artifacts(self) -> bool:
        return self.control is not None or bool(self.config.getoption("--kensa-write-artifacts"))

    def set_active_trial(self, active_trial: ActiveTrial | None) -> None:
        if self.control_path is None or self.control is None:
            return
        self.control = replace(self.control, active_trial=active_trial)
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
    if config.getoption("--kensa-control-path") and _distributed_pytest_enabled(config):
        raise pytest.UsageError(DISTRIBUTED_PYTEST_UNSUPPORTED)
    config.addinivalue_line(
        "markers",
        "kensa(trials=1, timeout_s=None): mark a Kensa agent eval",
    )
    _state(config)
    ensure_tracing()


def _distributed_pytest_enabled(config: pytest.Config) -> bool:
    numprocesses = config.getoption("numprocesses", default=0)
    dist = config.getoption("dist", default="no")
    tx = config.getoption("tx", default=[])
    distload = config.getoption("distload", default=False)
    return bool(numprocesses) or bool(distload) or (dist != "no" and bool(tx))


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
        snapshot_callback=lambda completed: _record_trial(
            item.config,
            completed.metadata(
                status=_PROVISIONAL_STATUS,
                duration_ms=completed.trace.duration_ms,
            ),
        ),
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
    if report.when not in {"setup", "teardown"} or not report.failed:
        return
    runtime = _runtime_for_item(item)
    if runtime is None:
        return
    if report.when == "setup":
        existing = next(
            (trial for trial in _state(item.config).trials if trial.nodeid == item.nodeid),
            None,
        )
        if existing is not None and existing.status != _PROVISIONAL_STATUS:
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
    state = _state(config)
    upsert_trial(state.trials, metadata)
    if state.write_artifacts:
        _write_artifacts(state)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    state = _state(session.config)
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
    )


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
