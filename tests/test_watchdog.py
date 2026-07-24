from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from kensa import cli, watchdog
from kensa.artifacts import load_trials
from kensa.cli import main
from kensa.runtime import ActiveOperation
from kensa.watchdog import (
    DEFAULT_JUDGE_TIMEOUT_S,
    DEFAULT_TRIAL_TIMEOUT_S,
    ActiveTrial,
    EvalProcessResult,
    WatchdogControl,
    control_paths,
    format_heartbeat,
    read_control,
    remove_control_files,
    validate_timeout_s,
    worker_control_path,
    write_control,
)


def _write_eval(tmp_path: Path, source: str) -> None:
    eval_dir = tmp_path / "tests" / "evals"
    eval_dir.mkdir(parents=True)
    (eval_dir / "conftest.py").write_text(
        """import pytest
from kensa.pytest import ConversationResponse


@pytest.fixture
def kensa_run(case):
    class Agent:
        def respond(self, messages):
            return ConversationResponse(output={"input": case.input})
    return Agent()
"""
    )
    (eval_dir / "test_timeout.py").write_text(source)


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_eval_timeout_bounds_full_pytest_item(
    phase: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = {
        "setup": "time.sleep(10)\n    yield",
        "call": "yield",
        "teardown": "yield\n    time.sleep(10)",
    }[phase]
    call = "time.sleep(10)" if phase == "call" else "case.run(kensa_run)"
    _write_eval(
        tmp_path,
        f"""import time
import pytest
from kensa.pytest import kensa_case


@pytest.fixture
def hanging_fixture():
    {fixture}


@pytest.mark.kensa(trials=1, timeout_s=0.15)
@pytest.mark.parametrize("case", [kensa_case(id="bounded", input="hello")])
def test_bounded(case, kensa_run, hanging_fixture):
    {call}
""",
    )

    started = time.monotonic()
    code = main(["eval", "--trial-timeout", "5", "--json", "tests/evals"])
    elapsed = time.monotonic() - started

    payload = json.loads(capsys.readouterr().out)
    artifact = Path(payload["data"]["artifact"])
    result = json.loads(artifact.read_text())
    trial = result["trials"][0]
    assert code == 1
    assert elapsed < 2
    assert payload["summary"] == "Kensa eval timed out."
    assert payload["data"]["timeout"] == {
        "case_id": "bounded",
        "trial_index": 1,
        "timeout_s": 0.15,
        "phase": phase,
    }
    assert trial["status"] == "error"
    assert trial["error_kind"] == ("timeout" if phase == "call" else phase)
    assert trial["trace"]["incomplete"] is True
    assert result["complete"] is False
    assert result["interruption"]["kind"] == "timeout"
    assert list((tmp_path / ".kensa" / "state").glob("*.json")) == []
    if phase == "teardown":
        assert trial["output"]["output"] == {"input": "hello"}
        assert len(result["trials"]) == 1
    if phase in {"setup", "teardown"}:
        assert result["summary"]["eligible_agent_trials"] == 0
        assert result["summary"]["pass_k_curve"] == []


def test_eval_timeout_preserves_prior_trials_and_kills_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    stale = tmp_path / ".kensa" / "results" / "stale.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps({"run_id": "stale", "trials": [], "aggregates": []}))
    _write_eval(
        tmp_path,
        """import subprocess
import sys
import time
from pathlib import Path

import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=3, timeout_s=0.2)
@pytest.mark.parametrize("case", [kensa_case(id="three_trials", input="hello")])
def test_three_trials(case, kensa_run, request):
    result = case.run(kensa_run)
    if "trial2" in request.node.nodeid:
        child = subprocess.Popen([
            sys.executable,
            "-c",
            "import signal, time; "
            "from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "Path('child.ready').write_text('ready'); "
            "time.sleep(60)",
        ])
        Path("child.pid").write_text(str(child.pid))
        while not Path("child.ready").exists():
            time.sleep(0.005)
        print("x" * 200_000, flush=True)
        time.sleep(60)
    assert result.output == {"input": "hello"}
""",
    )

    code = main(["eval", "--workers", "1", "--json", "tests/evals", "--", "-s"])

    payload = json.loads(capsys.readouterr().out)
    artifact = Path(payload["data"]["artifact"])
    result = json.loads(artifact.read_text())
    aggregate = result["aggregates"][0]
    child_pid = int((tmp_path / "child.pid").read_text())
    assert code == 1
    assert artifact != stale
    assert len(result["trials"]) == 2
    assert result["trials"][0]["status"] == "pass"
    assert result["trials"][1]["error_kind"] == "timeout"
    assert 0 < result["trials"][1]["duration_ms"] < 1000
    assert aggregate["verdict"] == "error"
    assert aggregate["partial"] is True
    assert "x" * 1000 in payload["data"]["pytest"]["stdout"]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"descendant process {child_pid} survived watchdog termination")


def test_eval_timeout_monitors_parallel_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_eval(
        tmp_path,
        """import time

import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(timeout_s=0.5)
@pytest.mark.parametrize("case", [
    kensa_case(id="hang", input="hang"),
    kensa_case(id="fast_a", input="a"),
    kensa_case(id="fast_b", input="b"),
    kensa_case(id="fast_c", input="c"),
])
def test_parallel_timeout(case, kensa_run):
    result = case.run(kensa_run)
    if case.id == "hang":
        time.sleep(60)
    assert result.output == {"input": case.input}
""",
    )

    started = time.monotonic()
    code = main(["eval", "--workers", "2", "--json", "tests/evals"])

    elapsed = time.monotonic() - started
    payload = json.loads(capsys.readouterr().out)
    result = json.loads(Path(payload["data"]["artifact"]).read_text())
    trials = {trial["case_id"]: trial for trial in result["trials"]}
    sort_keys = [
        (trial["group_id"], trial["trial_index"], trial["nodeid"]) for trial in result["trials"]
    ]
    assert code == 1
    assert elapsed < 3
    assert payload["data"]["workers"] == 2
    assert payload["data"]["timeout"] == {
        "case_id": "hang",
        "trial_index": 1,
        "timeout_s": 0.5,
        "phase": "call",
    }
    assert "hang" in trials
    assert set(trials) <= {"hang", "fast_a", "fast_b", "fast_c"}
    assert sort_keys == sorted(sort_keys)
    assert trials["hang"]["error_kind"] == "timeout"
    passed = {case_id for case_id, trial in trials.items() if trial["status"] == "pass"}
    assert passed
    assert passed <= {"fast_a", "fast_b", "fast_c"}
    sibling_trials = [trial for case_id, trial in trials.items() if case_id != "hang"]
    assert all(trial["error_kind"] is None for trial in sibling_trials)
    assert result["complete"] is False
    assert result["interruption"] == {
        "kind": "timeout",
        "message": "Kensa timeout: hang trial 1 exceeded 0.5 seconds.",
        "nodeid": trials["hang"]["nodeid"],
        "case_id": "hang",
        "trial_index": 1,
        "phase": "call",
    }
    assert list((tmp_path / ".kensa" / "state").glob("*.json")) == []


def test_parallel_timeout_preserves_reported_sibling_call_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_eval(
        tmp_path,
        """import os
import time
from pathlib import Path

import pytest
from kensa.pytest import kensa_case


@pytest.fixture
def hanging_teardown():
    yield
    Path("teardown.started").write_text("yes")
    time.sleep(60)


@pytest.mark.kensa(timeout_s=5)
@pytest.mark.parametrize("case", [kensa_case(id="teardown_active", input="fast")])
def test_teardown_active(case, kensa_run, hanging_teardown):
    Path("teardown.worker").write_text(os.environ["PYTEST_XDIST_WORKER"])
    assert case.run(kensa_run).output == {"input": "fast"}


@pytest.mark.kensa(timeout_s=1)
@pytest.mark.parametrize("case", [kensa_case(id="call_timeout", input="slow")])
def test_call_timeout(case):
    Path("timeout.worker").write_text(os.environ["PYTEST_XDIST_WORKER"])
    time.sleep(60)
""",
    )
    conftest = tmp_path / "tests" / "evals" / "conftest.py"
    conftest.write_text(
        conftest.read_text()
        + """

import os
from pathlib import Path


@pytest.hookimpl(trylast=True)
def pytest_runtest_logreport(report):
    if (
        os.environ.get("PYTEST_XDIST_WORKER") is None
        and report.when == "call"
        and "teardown_active" in report.nodeid
    ):
        Path("teardown_pass.reported").write_text("yes")
"""
    )

    code = main(["eval", "--workers", "2", "--json", "tests/evals"])

    payload = json.loads(capsys.readouterr().out)
    result = json.loads(Path(payload["data"]["artifact"]).read_text())
    trials = {trial["case_id"]: trial for trial in result["trials"]}
    assert code == 1
    assert (tmp_path / "teardown.started").is_file()
    assert (tmp_path / "teardown_pass.reported").is_file()
    assert (tmp_path / "teardown.worker").read_text() != (tmp_path / "timeout.worker").read_text()
    assert trials["call_timeout"]["error_kind"] == "timeout"
    assert trials["teardown_active"]["error_kind"] is None
    assert trials["teardown_active"]["status"] == "pass"
    assert result["complete"] is False


@pytest.mark.parametrize("source", ["config", "environment"])
def test_workers_one_rejects_resolved_xdist_worker_override(
    source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    if source == "config":
        (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "-n 2"\n')
    else:
        monkeypatch.setenv("PYTEST_ADDOPTS", "-n 2")
    _write_eval(
        tmp_path,
        """import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa
@pytest.mark.parametrize("case", [kensa_case(id="sequential", input="hello")])
def test_sequential(case, kensa_run):
    case.run(kensa_run)
""",
    )

    assert main(["eval", "--workers", "1", "tests/evals"]) == 2

    assert "expected 1 local pytest worker, but pytest resolved 2" in capfd.readouterr().err


def test_eval_json_reports_resolved_worker_mismatch_as_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n 4")
    _write_eval(
        tmp_path,
        """import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa
@pytest.mark.parametrize("case", [kensa_case(id="sequential", input="hello")])
def test_sequential(case, kensa_run):
    case.run(kensa_run)
""",
    )

    code = main(["eval", "--workers", "1", "--json", "tests/evals"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["exit_code"] == 2
    assert payload["summary"] == "Kensa eval received invalid pytest configuration."
    assert payload["warnings"] == []
    assert payload["next_steps"] == []
    assert payload["data"]["pytest"]["returncode"] == 4
    assert len(payload["errors"]) == 1
    assert "expected 1 local pytest worker, but pytest resolved 4" in payload["errors"][0]


def test_eval_rejects_resolved_xdist_worker_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "--maxprocesses=2"\n'
    )
    _write_eval(
        tmp_path,
        """import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa
@pytest.mark.parametrize("case", [kensa_case(id="limited", input="hello")])
def test_limited(case, kensa_run):
    case.run(kensa_run)
""",
    )

    assert main(["eval", "--workers", "4", "tests/evals"]) == 2

    assert "expected 4 local pytest workers, but pytest resolved 2" in capfd.readouterr().err


@pytest.mark.parametrize(
    "addopts",
    ["-d --tx ssh=example.invalid", "--px socket=example.invalid:8888"],
)
def test_eval_rejects_configured_remote_xdist_gateways(
    addopts: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(f'[tool.pytest.ini_options]\naddopts = "{addopts}"\n')
    _write_eval(
        tmp_path,
        """import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa
@pytest.mark.parametrize("case", [kensa_case(id="local", input="hello")])
def test_local(case, kensa_run):
    case.run(kensa_run)
""",
    )

    assert main(["eval", "--workers", "1", "tests/evals"]) == 2

    assert "supports local pytest workers only" in capfd.readouterr().err


def test_eval_timeout_does_not_wait_for_detached_descendant_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(watchdog, "WATCHDOG_OUTPUT_DRAIN_TIMEOUT_S", 0.05)
    _write_eval(
        tmp_path,
        """import subprocess
import sys
import time
from pathlib import Path

import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(timeout_s=0.15)
@pytest.mark.parametrize("case", [kensa_case(id="detached", input="hello")])
def test_detached(case):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    Path("detached.pid").write_text(str(child.pid))
    print("output before timeout", flush=True)
    print("error before timeout", file=sys.stderr, flush=True)
    time.sleep(60)
""",
    )

    started = time.monotonic()
    try:
        code = main(["eval", "--workers", "1", "--json", "tests/evals", "--", "-s"])
    finally:
        child_pid = int((tmp_path / "detached.pid").read_text())
        with suppress(ProcessLookupError):
            os.kill(child_pid, signal.SIGKILL)

    elapsed = time.monotonic() - started
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert elapsed < 2
    assert payload["data"]["timeout"]["case_id"] == "detached"
    assert "output before timeout" in payload["data"]["pytest"]["stdout"]
    assert "error before timeout" in payload["data"]["pytest"]["stderr"]


def test_eval_success_does_not_wait_for_background_descendant_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_eval(
        tmp_path,
        """import subprocess
import sys
import time
from pathlib import Path

import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa
@pytest.mark.parametrize("case", [kensa_case(id="background", input="hello")])
def test_background(case, kensa_run):
    assert case.run(kensa_run).output == {"input": "hello"}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path("background.pid").write_text(str(child.pid))
    print("output before success", flush=True)
""",
    )

    started = time.monotonic()
    pid_path = tmp_path / "background.pid"
    child_pid: int | None = None
    try:
        code = main(["eval", "--workers", "1", "--json", "tests/evals", "--", "-s"])
        child_pid = int(pid_path.read_text())
        elapsed = time.monotonic() - started
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert elapsed < 2
        assert payload["data"]["timeout"] is None
        assert "output before success" in payload["data"]["pytest"]["stdout"]

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"background process {child_pid} survived watchdog cleanup")
    finally:
        if child_pid is None and pid_path.exists():
            child_pid = int(pid_path.read_text())
        if child_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)


def test_eval_timeout_records_active_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_eval(
        tmp_path,
        """import time
import pytest
from kensa import record_llm_call
from kensa.pytest import kensa_case


@pytest.mark.kensa(timeout_s=0.15)
@pytest.mark.parametrize("case", [kensa_case(id="active_case", input="hello")])
def test_active_operation(case):
    with record_llm_call(
        "customer_simulator.turn",
        provider="test",
        model="test-model",
        attributes={"attempt": 1, "turn": 2},
    ):
        time.sleep(60)
""",
    )

    assert main(["eval", "--json", "tests/evals"]) == 1

    payload = json.loads(capsys.readouterr().out)
    artifact = json.loads(Path(payload["data"]["artifact"]).read_text())
    assert artifact["trials"][0]["active_operation"] == {
        "name": "customer_simulator.turn",
        "kind": "llm",
        "attributes": {
            "attempt": 1,
            "turn": 2,
            "provider": "test",
            "model": "test-model",
        },
    }
    assert artifact["summary"]["cost_latency"]["cost_relevant_trials"] == 1
    assert artifact["summary"]["cost_latency"]["cost_complete"] is False


def test_eval_timeout_preserves_completed_llm_cost_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_eval(
        tmp_path,
        """import time

import pytest
from kensa import record_llm_call
from kensa.pytest import ConversationResponse, kensa_case


@pytest.fixture
def priced_agent(request):
    class Agent:
        def respond(self, messages):
            with record_llm_call(attributes={"kensa.cost_usd": 0.2}):
                pass
            if "trial2" in request.node.nodeid:
                time.sleep(60)
            return ConversationResponse(output={"ok": True})

    return Agent()


@pytest.mark.kensa(trials=2, timeout_s=0.25)
@pytest.mark.parametrize("case", [kensa_case(id="priced", input="hello")])
def test_priced(case, priced_agent):
    case.run(priced_agent)
""",
    )

    assert main(["eval", "--workers", "1", "--json", "tests/evals"]) == 1

    payload = json.loads(capsys.readouterr().out)
    artifact = json.loads(Path(payload["data"]["artifact"]).read_text())
    trials = artifact["trials"]
    cost = artifact["summary"]["cost_latency"]
    assert [trial["status"] for trial in trials] == ["pass", "error"]
    assert trials[1]["error_kind"] == "timeout"
    assert trials[1]["trace"]["llm_turns"] == 1
    assert trials[1]["trace"]["known_cost_usd"] == 0.2
    assert trials[1]["trace"]["cost_usd"] == 0.2
    assert cost["known_cost_usd"] == pytest.approx(0.4)
    assert cost["total_cost_usd"] == pytest.approx(0.4)
    assert cost["cost_known_trials"] == 2
    assert cost["cost_relevant_trials"] == 2
    assert cost["cost_coverage"] == 1.0
    assert cost["cost_complete"] is True


def test_eval_timeout_preserves_instrumented_genai_cost_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_eval(
        tmp_path,
        """import time

import pytest
from opentelemetry import trace
from kensa.pytest import ConversationResponse, kensa_case


@pytest.fixture
def priced_agent(request):
    class Agent:
        def respond(self, messages):
            tracer = trace.get_tracer("instrumented-genai")
            with tracer.start_as_current_span(
                "chat test-model",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": "test",
                    "gen_ai.request.model": "test-model",
                    "kensa.cost_usd": 0.2,
                },
            ):
                pass
            if "trial2" in request.node.nodeid:
                time.sleep(60)
            return ConversationResponse(output={"ok": True})

    return Agent()


@pytest.mark.kensa(trials=2, timeout_s=0.25)
@pytest.mark.parametrize("case", [kensa_case(id="priced", input="hello")])
def test_priced(case, priced_agent):
    case.run(priced_agent)
""",
    )

    assert main(["eval", "--workers", "1", "--json", "tests/evals"]) == 1

    payload = json.loads(capsys.readouterr().out)
    artifact = json.loads(Path(payload["data"]["artifact"]).read_text())
    trials = artifact["trials"]
    cost = artifact["summary"]["cost_latency"]
    assert [trial["status"] for trial in trials] == ["pass", "error"]
    assert trials[1]["error_kind"] == "timeout"
    assert trials[1]["trace"]["llm_turns"] == 1
    assert trials[1]["trace"]["known_cost_usd"] == 0.2
    assert trials[1]["trace"]["cost_usd"] == 0.2
    assert cost["known_cost_usd"] == pytest.approx(0.4)
    assert cost["total_cost_usd"] == pytest.approx(0.4)
    assert cost["cost_known_trials"] == 2
    assert cost["cost_relevant_trials"] == 2
    assert cost["cost_coverage"] == 1.0
    assert cost["cost_complete"] is True


def test_eval_timeout_marks_open_instrumented_genai_cost_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_eval(
        tmp_path,
        """import time

import pytest
from opentelemetry import trace
from kensa.pytest import ConversationResponse, kensa_case


@pytest.fixture
def priced_agent(request):
    class Agent:
        def respond(self, messages):
            tracer = trace.get_tracer("instrumented-genai")
            with tracer.start_as_current_span(
                "chat test-model",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": "test",
                    "gen_ai.request.model": "test-model",
                    "kensa.cost_usd": 0.2,
                },
            ):
                if "trial2" in request.node.nodeid:
                    time.sleep(60)
            return ConversationResponse(output={"ok": True})

    return Agent()


@pytest.mark.kensa(trials=2, timeout_s=0.25)
@pytest.mark.parametrize("case", [kensa_case(id="priced", input="hello")])
def test_priced(case, priced_agent):
    case.run(priced_agent)
""",
    )

    assert main(["eval", "--workers", "1", "--json", "tests/evals"]) == 1

    payload = json.loads(capsys.readouterr().out)
    artifact = json.loads(Path(payload["data"]["artifact"]).read_text())
    trials = artifact["trials"]
    cost = artifact["summary"]["cost_latency"]
    assert [trial["status"] for trial in trials] == ["pass", "error"]
    assert trials[1]["error_kind"] == "timeout"
    assert trials[1]["active_operation"] == {
        "name": "chat test-model",
        "kind": "llm",
        "attributes": {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "test",
            "gen_ai.request.model": "test-model",
            "kensa.cost_usd": 0.2,
        },
    }
    assert trials[1]["trace"]["llm_turns"] == 0
    assert cost["known_cost_usd"] == pytest.approx(0.2)
    assert cost["total_cost_usd"] is None
    assert cost["cost_known_trials"] == 1
    assert cost["cost_relevant_trials"] == 2
    assert cost["cost_coverage"] == 0.5
    assert cost["cost_complete"] is False


def test_parallel_timeout_preserves_published_trial_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KENSA_JUDGE_RESULT", "pass")
    _write_eval(
        tmp_path,
        """import time

import pytest
from kensa.pytest import judge, kensa_case


@pytest.mark.kensa(timeout_s=0.5)
@pytest.mark.parametrize("case", [kensa_case(id="snapshot", input="hello")])
def test_snapshot(case, kensa_run):
    result = case.run(kensa_run)
    verdict = judge(result, "must preserve evidence")
    assert verdict.passed
    time.sleep(60)
""",
    )

    assert main(["eval", "--json", "tests/evals"]) == 1

    payload = json.loads(capsys.readouterr().out)
    artifact = json.loads(Path(payload["data"]["artifact"]).read_text())
    trial = artifact["trials"][0]
    assert payload["data"]["workers"] == 4
    assert trial["status"] == "error"
    assert trial["error_kind"] == "timeout"
    assert trial["case"] == {"id": "snapshot", "input": "hello"}
    assert trial["output"]["output"] == {"input": "hello"}
    assert trial["trace"]["spans"][0]["name"] == "kensa.pytest.trial"
    assert trial["trace"]["incomplete"] is True
    assert trial["judges"] == [
        {
            "passed": True,
            "reasoning": "Environment judge returned pass for: must preserve evidence",
            "evidence": [],
            "provider": "env",
            "model": "KENSA_JUDGE_RESULT",
            "metadata": {},
            "error": False,
        }
    ]


def test_parallel_timeout_prefers_teardown_snapshot_over_reported_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KENSA_JUDGE_RESULT", "pass")
    _write_eval(
        tmp_path,
        """import time
from pathlib import Path

import pytest
from kensa.pytest import judge, kensa_case


@pytest.fixture
def judging_teardown():
    yield
    while not Path("call.reported").exists():
        time.sleep(0.01)
    result = judge({"input": "hello"}, "preserve teardown evidence")
    assert result.passed
    Path("judge.recorded").write_text("yes")
    time.sleep(60)


@pytest.mark.kensa(timeout_s=1)
@pytest.mark.parametrize("case", [kensa_case(id="teardown_snapshot", input="hello")])
def test_teardown_snapshot(case, kensa_run, judging_teardown):
    assert case.run(kensa_run).output == {"input": "hello"}
""",
    )
    conftest = tmp_path / "tests" / "evals" / "conftest.py"
    conftest.write_text(
        conftest.read_text()
        + """

import os
from pathlib import Path


@pytest.hookimpl(trylast=True)
def pytest_runtest_logreport(report):
    if (
        os.environ.get("PYTEST_XDIST_WORKER") is None
        and report.when == "call"
        and "teardown_snapshot" in report.nodeid
    ):
        Path("call.reported").write_text("yes")
"""
    )

    assert main(["eval", "--workers", "2", "--json", "tests/evals"]) == 1

    payload = json.loads(capsys.readouterr().out)
    artifact = json.loads(Path(payload["data"]["artifact"]).read_text())
    trial = artifact["trials"][0]
    assert (tmp_path / "call.reported").is_file()
    assert (tmp_path / "judge.recorded").is_file()
    assert trial["status"] == "error"
    assert trial["error_kind"] == "teardown"
    assert artifact["summary"]["eligible_agent_trials"] == 0
    assert trial["judges"] == [
        {
            "passed": True,
            "reasoning": "Environment judge returned pass for: preserve teardown evidence",
            "evidence": [],
            "provider": "env",
            "model": "KENSA_JUDGE_RESULT",
            "metadata": {},
            "error": False,
        }
    ]


@pytest.mark.parametrize("value", [True, False, 0, -1, float("nan"), float("inf")])
def test_validate_timeout_rejects_invalid_values(value: Any) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        validate_timeout_s(value)


def test_eval_cli_passes_default_timeout_through_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    observed: list[tuple[float, float]] = []

    def fake_run(*args: Any, **kwargs: Any) -> EvalProcessResult:
        del args
        control = read_control(Path(kwargs["control_path"]))
        observed.append((control.default_timeout_s, control.judge_timeout_s))
        return EvalProcessResult(returncode=5)

    monkeypatch.setattr(cli, "run_eval_process", fake_run)

    assert main(["eval", "--json"]) == 5
    assert json.loads(capsys.readouterr().out)["data"]["timeout"] is None
    assert main(["eval", "--json", "--trial-timeout=12", "--judge-timeout=4"]) == 5
    assert json.loads(capsys.readouterr().out)["data"]["timeout"] is None
    assert observed == [
        (DEFAULT_TRIAL_TIMEOUT_S, DEFAULT_JUDGE_TIMEOUT_S),
        (12, 4),
    ]


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_eval_cli_rejects_invalid_timeout(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["eval", f"--trial-timeout={value}"]) == 2
    assert "positive finite number" in capsys.readouterr().err

    assert main(["eval", f"--judge-timeout={value}"]) == 2
    assert "positive finite number" in capsys.readouterr().err


def test_eval_rejects_control_path_passthrough(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "run_eval_process",
        lambda *args, **kwargs: pytest.fail("pytest should not launch"),
    )

    assert main(["eval", "--json", "--", "--kensa-control-path="]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"] == "Kensa eval received a reserved pytest option."
    assert payload["errors"] == [
        "--kensa-control-path is reserved for kensa eval and cannot be passed to pytest."
    ]

    assert main(["eval", "--", "--kensa-control-path", "ignored.json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--kensa-control-path is reserved" in captured.err
    assert not (tmp_path / ".kensa").exists()


def test_eval_rejects_unsupported_platform_before_launch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "supported_watchdog_platform", lambda: False)
    monkeypatch.setattr(
        cli,
        "run_eval_process",
        lambda *args, **kwargs: pytest.fail("pytest should not launch"),
    )

    assert main(["eval", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"] == ["kensa eval trial timeouts require macOS or Linux."]

    assert main(["eval"]) == 2
    assert "require macOS or Linux" in capsys.readouterr().err


def test_eval_terminal_timeout_prints_reason_and_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_eval(
        tmp_path,
        """import time
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(timeout_s=0.15)
@pytest.mark.parametrize("case", [kensa_case(id="terminal_case", input="hello")])
def test_terminal_timeout(case):
    time.sleep(10)
""",
    )

    assert main(["eval", "tests/evals"]) == 1

    captured = capfd.readouterr()
    assert "Kensa timeout: terminal_case trial 1 exceeded 0.15 seconds." in captured.err
    assert "Artifact: .kensa/results/" in captured.out


def test_watchdog_rejects_invalid_control_and_artifact(
    tmp_path: Path,
) -> None:
    control_path = tmp_path / "control.json"
    control_path.write_text("[]")
    with pytest.raises(ValueError, match="Invalid Kensa watchdog control"):
        read_control(control_path)
    with pytest.raises(ValueError, match="active trial phase"):
        watchdog._trial_phase({})
    with pytest.raises(ValueError, match="active operation kind"):
        watchdog._operation_kind("invalid")

    result_path = tmp_path / "result.json"
    result_path.write_text('{"trials": {}}')
    with pytest.raises(ValueError, match="invalid trials"):
        load_trials(result_path)


def test_worker_control_paths_are_isolated_and_removed(tmp_path: Path) -> None:
    root = tmp_path / "state" / "run.json"
    control = WatchdogControl(
        run_id="run",
        result_path=tmp_path / "results" / "run.json",
        artifact_dir=tmp_path,
        default_timeout_s=30,
    )
    worker_a = worker_control_path(root, "gw0")
    worker_b = worker_control_path(root, "gw1")
    write_control(root, control)
    write_control(worker_a, control)
    write_control(worker_b, control)

    assert control_paths(root) == [root, worker_a, worker_b]
    with pytest.raises(ValueError, match="Invalid pytest-xdist worker ID"):
        worker_control_path(root, "../gw2")

    remove_control_files(root)

    assert control_paths(root) == []


def test_watchdog_cleans_up_when_monitoring_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = object()
    terminated: list[object] = []
    control_path = tmp_path / "control.json"
    control_path.write_text("{}")
    monkeypatch.setattr(watchdog.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        watchdog,
        "read_control",
        lambda path: (_ for _ in ()).throw(RuntimeError("control failed")),
    )
    monkeypatch.setattr(watchdog, "_terminate_process_group", terminated.append)

    with pytest.raises(RuntimeError, match="control failed"):
        watchdog.run_eval_process(
            ["pytest"],
            control_path=control_path,
            capture_output=False,
        )
    assert terminated == [process]


def test_watchdog_tolerates_worker_control_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = object()
    control_path = tmp_path / "control.json"
    active = ActiveTrial(
        nodeid="test.py::test_case[trial1]",
        group_id="test.py::test_case",
        case_id="case",
        trial_index=1,
        configured_trials=1,
        timeout_s=1,
        started_monotonic_ns=0,
    )
    control = WatchdogControl(
        run_id="run",
        result_path=tmp_path / "result.json",
        artifact_dir=tmp_path,
        default_timeout_s=1,
        active_trial=active,
    )
    completed = EvalProcessResult(returncode=0)
    monkeypatch.setattr(watchdog.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(watchdog, "control_paths", lambda path: [control_path])
    monkeypatch.setattr(watchdog, "_wait_once", lambda *args: completed)

    monkeypatch.setattr(
        watchdog,
        "read_control",
        lambda path: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert (
        watchdog.run_eval_process(["pytest"], control_path=control_path, capture_output=False)
        == completed
    )

    controls = iter([control, FileNotFoundError()])

    def disappearing_control(path: Path) -> WatchdogControl:
        del path
        value = next(controls)
        if isinstance(value, FileNotFoundError):
            raise value
        return value

    monkeypatch.setattr(watchdog, "read_control", disappearing_control)
    monkeypatch.setattr(watchdog, "_trial_expired", lambda trial: True)
    assert (
        watchdog.run_eval_process(["pytest"], control_path=control_path, capture_output=False)
        == completed
    )


def test_process_group_termination_edge_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        pid = 123

        def __init__(self, *, running: bool = True) -> None:
            self.running = running
            self.waits = 0

        def poll(self) -> int | None:
            return None if self.running else 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.waits += 1
            self.running = False
            return 0

    finished = Process(running=False)
    monkeypatch.setattr(watchdog, "_process_group_exists", lambda pid: False)
    watchdog._terminate_process_group(cast(Any, finished))
    assert finished.waits == 0

    missing = Process()
    monkeypatch.setattr(watchdog, "_process_group_exists", lambda pid: True)
    monkeypatch.setattr(
        watchdog.os,
        "killpg",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    watchdog._terminate_process_group(cast(Any, missing))
    assert missing.waits == 1

    killed = Process()
    signals: list[signal.Signals] = []

    def fake_killpg(pid: int, sent_signal: signal.Signals) -> None:
        del pid
        signals.append(sent_signal)

    monkeypatch.setattr(watchdog, "WATCHDOG_TERMINATION_GRACE_S", 0)
    monkeypatch.setattr(watchdog.os, "killpg", fake_killpg)
    watchdog._terminate_process_group(cast(Any, killed))
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert killed.waits == 1
    assert watchdog._collect_output(cast(Any, killed), False) == ("", "")

    disappeared = Process()
    group_states = iter([True, False, False])
    monkeypatch.setattr(watchdog, "_process_group_exists", lambda pid: next(group_states))
    watchdog._terminate_process_group(cast(Any, disappeared))
    assert disappeared.waits == 1


def test_output_collection_closes_pipes_when_drain_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pipe:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Process:
        def __init__(self) -> None:
            self.stdout = Pipe()
            self.stderr = Pipe()

        def communicate(self, timeout: float) -> tuple[str, str]:
            raise subprocess.TimeoutExpired(
                ["pytest"],
                timeout,
                output=b"partial stdout",
                stderr=b"partial stderr",
            )

    process = Process()
    monkeypatch.setattr(watchdog, "WATCHDOG_OUTPUT_DRAIN_TIMEOUT_S", 0.25)

    assert watchdog._collect_output(cast(Any, process), True) == (
        "partial stdout",
        "partial stderr",
    )
    assert process.stdout.closed
    assert process.stderr.closed


def test_process_group_probe_handles_missing_and_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        watchdog.os,
        "killpg",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    assert not watchdog._process_group_exists(123)

    monkeypatch.setattr(
        watchdog.os,
        "killpg",
        lambda pid, sig: (_ for _ in ()).throw(PermissionError()),
    )
    assert watchdog._process_group_exists(123)


def test_heartbeat_reports_active_operation_once_per_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = ActiveTrial(
        nodeid="test.py::test_case[trial1]",
        group_id="test.py::test_case",
        case_id="bounded",
        trial_index=1,
        configured_trials=1,
        timeout_s=30,
        started_monotonic_ns=1_000_000_000,
        active_operation=ActiveOperation(
            name="customer_simulator.turn",
            attributes={"attempt": 1, "model": "test-model"},
        ),
    )
    messages: list[str] = []
    monkeypatch.setattr(watchdog.time, "monotonic_ns", lambda: 11_500_000_000)

    marker = watchdog._emit_heartbeat(active, messages.append, None)
    duplicate = watchdog._emit_heartbeat(active, messages.append, marker)

    assert duplicate == marker
    assert messages == [
        "bounded trial 1 | 10s | customer_simulator.turn | attempt=1 model=test-model"
    ]
    assert format_heartbeat(replace(active, active_operation=None), 2) == "bounded trial 1 | 2s"


def test_timeout_elapsed_uses_call_phase_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = ActiveTrial(
        nodeid="test.py::test_case[trial1]",
        group_id="test.py::test_case",
        case_id="bounded",
        trial_index=1,
        configured_trials=1,
        timeout_s=1,
        started_monotonic_ns=1_000_000_000,
        call_started_monotonic_ns=1_120_000_000,
        phase="call",
    )
    monkeypatch.setattr(watchdog.time, "monotonic_ns", lambda: 1_273_000_000)

    assert watchdog._trial_elapsed_ms(active) == 153
    assert watchdog._trial_elapsed_ms(replace(active, phase="setup")) == 273


def test_heartbeat_redacts_and_limits_attributes() -> None:
    active = ActiveTrial(
        nodeid="test.py::test_case[trial1]",
        group_id="test.py::test_case",
        case_id="customer@example.com",
        trial_index=1,
        configured_trials=1,
        timeout_s=30,
        started_monotonic_ns=0,
        active_operation=ActiveOperation(
            name="unsafe operation name",
            attributes={
                "api_key": "sk-should-not-appear",
                "attempt": 1_000_001,
                "model": "sk-proj-abcdefghijklmnopqrstuvwxyz",
                "payload": "customer data " * 1000,
                "prompt": "private prompt",
                "provider": "provider-" + "a" * 80,
                "turn": True,
            },
        ),
    )

    message = format_heartbeat(active, 10)

    assert message.startswith("[REDACTED] trial 1 | 10s | [REDACTED] | ")
    assert "api_key" not in message
    assert "payload" not in message
    assert "prompt" not in message
    assert "turn" not in message
    assert "attempt=[REDACTED]" in message
    assert "model=[REDACTED]" in message
    assert "provider=provider-" in message
    assert message.endswith("...")
    assert len(message) < 180


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("x" * 257, "[REDACTED]"),
        ("customer@example.com", "[REDACTED]"),
        ("AKIAIOSFODNN7EXAMPLE", "[REDACTED]"),
        ("eyJabc.def.ghi", "[REDACTED]"),
        ("abc123" * 6, "[REDACTED]"),
        ("model-" + "a" * 70, "model-" + "a" * 55 + "..."),
    ],
)
def test_heartbeat_text_sanitization(value: str, expected: str) -> None:
    assert watchdog._sanitize_heartbeat_text(value) == expected
