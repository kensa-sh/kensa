from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from typing import Any, cast

import pytest

import kensa.pytest_plugin as pytest_plugin
from kensa.case import kensa_case
from kensa.engine import (
    PROTOCOL_VERSION,
    EngineClient,
    KensaEngineError,
    _check_outcome,
    _engine_command,
    _engine_executable,
    _wire_json_value,
)
from kensa.errors import KensaCaseError, TrialFailure
from kensa.pytest_plugin import _classify_exception
from kensa.runtime import KensaTrial, KensaTrialRuntime, _engine_trace


class _Stream:
    def __init__(self, *lines: str, write_error: BaseException | None = None) -> None:
        self.lines = list(lines)
        self.write_error = write_error
        self.writes: list[str] = []
        self.closed = False

    def write(self, value: str) -> int:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(value)
        return len(value)

    def flush(self) -> None:
        return None

    def readline(self) -> str:
        return self.lines.pop(0) if self.lines else ""

    def close(self) -> None:
        self.closed = True


class _Process:
    def __init__(
        self,
        *responses: str,
        stdin: _Stream | None = None,
        wait_timeouts: int = 0,
    ) -> None:
        self.stdin: _Stream | None = stdin if stdin is not None else _Stream()
        self.stdout = _Stream(*responses)
        self.wait_timeouts = wait_timeouts
        self.terminated = False
        self.killed = False

    def wait(self, timeout: int | None = None) -> int:
        del timeout
        if self.wait_timeouts:
            self.wait_timeouts -= 1
            raise subprocess.TimeoutExpired("engine", 5)
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def poll(self) -> int:
        return 7


def _raw_client(process: _Process) -> Any:
    client = object.__new__(EngineClient)
    client._process = process
    client._lock = Lock()
    client._request_number = 0
    client._closed = False
    return client


def _response(response: Any, *, request_id: str = "1", ok: bool = True) -> str:
    envelope = (
        {"id": request_id, "ok": True, "response": response}
        if ok
        else {"id": request_id, "ok": False, "failure": response}
    )
    return json.dumps(envelope) + "\n"


def test_engine_client_runs_case_and_cancellation() -> None:
    with EngineClient() as client:
        client.start_case(
            "passing",
            {"id": "hello", "input": "world", "metadata": {"id": "hello"}},
        )
        verdict = client.complete_case(
            "passing",
            observation={
                "output": "hello world",
                "output_recorded": True,
                "trace": {
                    "spans": [],
                    "agent_runs": [],
                    "tools": [],
                    "tool_calls": [],
                    "incomplete": False,
                    "incomplete_reason": None,
                    "duration_ms": 0,
                    "cost_usd": None,
                    "known_cost_usd": None,
                    "cost_available": False,
                    "llm_turns": 0,
                },
                "failure": None,
            },
            status="pass",
            failure=None,
        )
        assert verdict == "pass"

        client.start_case(
            "cancelled",
            {"id": "cancelled", "input": None, "metadata": {}},
        )
        client.cancel_case("cancelled", "test stopped")


def test_engine_client_fails_closed_on_version_mismatch() -> None:
    command = _engine_command()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(
        json.dumps(
            {
                "id": "1",
                "request": {
                    "type": "handshake",
                    "protocol_version": f"{PROTOCOL_VERSION}.future",
                    "client": "pytest",
                },
            }
        )
        + "\n"
    )
    process.stdin.flush()
    response = json.loads(process.stdout.readline())
    process.stdin.close()
    process.wait(timeout=5)

    assert response["ok"] is False
    assert response["failure"]["code"] == "version_mismatch"


def test_engine_command_honors_explicit_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KENSA_ENGINE_COMMAND", "node 'engine path.js'")
    assert _engine_command() == ("node", "engine path.js")


def test_engine_error_exposes_stable_code() -> None:
    error = KensaEngineError("failed", code="crash", details={"status": 1})
    assert str(error) == "failed"
    assert error.code == "crash"
    assert error.details == {"status": 1}
    assert KensaEngineError("plain").details == {}


def test_engine_failure_classification_is_infrastructure() -> None:
    status, failure = _classify_exception(KensaEngineError("", code="crash"), phase="call")
    assert status == "error"
    assert failure == TrialFailure(
        category="infrastructure",
        kind="crash",
        message="Kensa engine failure",
        evidence={"exception_type": "KensaEngineError"},
    )


def test_python_eval_uses_one_engine_process(
    pytester: pytest.Pytester,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launches = tmp_path / "launches.txt"
    wrapper = tmp_path / "engine-wrapper.py"
    engine = _engine_command()
    wrapper.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import os",
                f"path = Path({str(launches)!r})",
                "path.write_text(path.read_text() + 'launch\\n' if path.exists() else 'launch\\n')",
                f"os.execv({engine[0]!r}, {list(engine)!r})",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "KENSA_ENGINE_COMMAND",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(wrapper))}",
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import ConversationResponse, kensa_case


class Agent:
    def respond(self, messages):
        return ConversationResponse(content="hello")


@pytest.mark.kensa
@pytest.mark.parametrize(
    "case",
    [kensa_case(id="one", input="hello"), kensa_case(id="two", input="hi")],
)
def test_agent(case):
    result = case.run(Agent())
    assert result.output == "hello"
"""
    )
    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=2)
    assert launches.read_text(encoding="utf-8").splitlines() == ["launch"]


def test_explicit_missing_engine_is_a_usage_error(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KENSA_ENGINE_COMMAND", str(tmp_path / "missing-engine"))
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case

@pytest.mark.kensa
@pytest.mark.parametrize("case", [kensa_case(id="one", input=None)])
def test_agent(case):
    case.run(lambda value: value)
"""
    )

    result = pytester.runpytest("-q")

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*Kensa engine startup failed: Could not start Kensa engine*"])
    assert "INTERNALERROR" not in result.stderr.str()


def test_engine_crash_during_finalize_records_infrastructure_failure(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = tmp_path / "crashing-engine.py"
    engine.write_text(
        """
import json
import sys

for line in sys.stdin:
    envelope = json.loads(line)
    request = envelope["request"]
    if request["type"] == "handshake":
        response = {
            "type": "handshake",
            "protocol_version": "kensa.engine.v1",
            "engine_version": "test",
        }
    elif request["type"] == "start_case":
        response = {"type": "action", "action": "invoke_agent", "case_id": "one"}
    else:
        raise SystemExit(9)
    print(json.dumps({"id": envelope["id"], "ok": True, "response": response}), flush=True)
""",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "KENSA_ENGINE_COMMAND",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(engine))}",
    )
    artifact_dir = tmp_path / "artifacts"
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case

@pytest.mark.kensa
@pytest.mark.parametrize("case", [kensa_case(id="one", input="hello")])
def test_agent(case):
    assert case.run(lambda value: value) == "hello"
"""
    )

    result = pytester.runpytest(
        "-q",
        "--kensa-write-artifacts",
        f"--kensa-artifact-dir={artifact_dir}",
    )

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Kensa engine failure: Kensa engine stopped before responding*"])
    result_files = list((artifact_dir / "results").glob("*.json"))
    assert len(result_files) == 1
    payload = json.loads(result_files[0].read_text(encoding="utf-8"))
    assert payload["trials"][0]["status"] == "error"
    assert payload["trials"][0]["failure"]["category"] == "infrastructure"
    assert payload["trials"][0]["failure"]["kind"] == "crash"


def test_engine_command_uses_built_development_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KENSA_ENGINE_COMMAND", raising=False)
    command = _engine_command()
    assert Path(command[-1]).name == "cli.js"
    assert os.path.isabs(command[-1])


def test_engine_command_resolves_bundled_and_missing_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KENSA_ENGINE_COMMAND", raising=False)
    monkeypatch.setattr(Path, "is_file", lambda path: path.parent.name == "bin")
    assert Path(_engine_command()[0]).parent.name == "bin"

    monkeypatch.setattr(Path, "is_file", lambda path: False)
    monkeypatch.setattr("kensa.engine.shutil.which", lambda name: None)
    with pytest.raises(KensaEngineError, match="executable is unavailable") as exc_info:
        _engine_command()
    assert exc_info.value.code == "startup"
    assert _engine_executable("nt") == "kensa-engine.exe"
    assert _engine_executable("posix") == "kensa-engine"


def test_engine_trace_preserves_nanosecond_timestamps_as_decimal_strings() -> None:
    original_span: dict[str, Any] = {
        "start_time_unix_nano": 1_786_430_000_000_000_000,
        "end_time_unix_nano": None,
    }
    original: dict[str, Any] = {
        "spans": [original_span, "ignored"],
        "agent_runs": [{"event": {"timestamp": 1_786_430_000_000_000_001}}],
    }

    wire = _engine_trace(original)

    assert wire["spans"][0]["start_time_unix_nano"] == "1786430000000000000"
    assert wire["spans"][0]["end_time_unix_nano"] is None
    assert wire["agent_runs"][0]["event"]["timestamp"] == "1786430000000000001"
    assert original_span["start_time_unix_nano"] == 1_786_430_000_000_000_000
    assert _engine_trace({"spans": None}) == {"spans": None}


def test_wire_json_value_preserves_types_and_safe_integers() -> None:
    assert _wire_json_value(
        {
            "values": [True, None, 1.5, "text", 9_007_199_254_740_991],
            1: (9_007_199_254_740_992,),
            "bytes": b"opaque",
        }
    ) == {
        "values": [True, None, 1.5, "text", 9_007_199_254_740_991],
        "1": ["9007199254740992"],
        "bytes": b"opaque",
    }


def test_engine_client_rejects_startup_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(KensaEngineError, match="must not be empty"):
        EngineClient(())

    def fail_start(*_: Any, **__: Any) -> None:
        raise OSError("missing")

    monkeypatch.setattr(subprocess, "Popen", fail_start)
    with pytest.raises(KensaEngineError, match="Could not start") as exc_info:
        EngineClient(("missing",))
    assert exc_info.value.code == "startup"


def test_engine_client_rejects_invalid_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _Process(_response({"type": "handshake", "protocol_version": "wrong"}))
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)

    with pytest.raises(KensaEngineError, match="invalid handshake") as exc_info:
        EngineClient(("engine",))

    assert exc_info.value.code == "handshake"
    assert process.stdin is not None
    assert process.stdin.closed


@pytest.mark.parametrize(
    ("method", "responses", "message"),
    [
        ("start", [{"type": "wrong"}], "agent invocation"),
        ("complete", [{"type": "wrong"}], "check evaluation"),
        (
            "complete",
            [{"type": "action", "action": "evaluate_check"}, {"type": "wrong"}],
            "invalid result",
        ),
        (
            "complete",
            [
                {"type": "action", "action": "evaluate_check"},
                {"type": "result", "evaluation": {"verdict": "maybe"}},
            ],
            "invalid verdict",
        ),
        (
            "complete",
            [
                {"type": "action", "action": "evaluate_check"},
                {
                    "type": "result",
                    "evaluation": {"phase": "complete", "verdict": "fail"},
                },
            ],
            "contradicts the check observation",
        ),
        ("cancel", [{"type": "result", "evaluation": {"phase": "complete"}}], "cancellation"),
    ],
)
def test_engine_client_rejects_invalid_protocol_actions(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    responses: list[dict[str, Any]],
    message: str,
) -> None:
    client = EngineClient()
    iterator = iter(responses)
    monkeypatch.setattr(client, "_request", lambda request: next(iterator))

    def invoke() -> None:
        if method == "start":
            client.start_case("eval", {})
        elif method == "complete":
            client.complete_case("eval", observation={}, status="pass", failure=None)
        else:
            client.cancel_case("eval", "stop")

    with pytest.raises(KensaEngineError, match=message):
        invoke()
    client.close()


def test_engine_client_rejects_contradictory_failure_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = EngineClient()
    responses = iter(
        [
            {"type": "action", "action": "evaluate_check"},
            {"type": "result", "evaluation": {"phase": "complete", "verdict": "pass"}},
        ]
    )
    monkeypatch.setattr(client, "_request", lambda request: next(responses))

    with pytest.raises(KensaEngineError, match="failure provenance"):
        client.complete_case(
            "eval",
            observation={},
            status="pass",
            failure={"category": "agent"},
        )
    with pytest.raises(KensaEngineError, match="Unknown check status"):
        _check_outcome("unknown")
    client.close()


def test_passing_test_engine_finalize_failure_is_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime:
        def finalize_engine(self, status: str, failure: Any) -> str:
            del status, failure
            raise KensaEngineError("stopped", code="crash")

    class Outcome:
        excinfo = None

        def __init__(self) -> None:
            self.exception: BaseException | None = None

        def force_exception(self, exception: BaseException) -> None:
            self.exception = exception

    state = SimpleNamespace(set_active_phase=lambda *_: None)
    item = SimpleNamespace(config=object(), nodeid="test_eval")
    recorded: list[KensaEngineError] = []
    with monkeypatch.context() as patcher:
        patcher.setattr(pytest_plugin, "_runtime_for_item", lambda _: Runtime())
        patcher.setattr(pytest_plugin, "_state", lambda _: state)
        patcher.setattr(
            pytest_plugin,
            "_record_engine_failure",
            lambda _item, _runtime, _duration, error: recorded.append(error),
        )
        hook = pytest_plugin.pytest_runtest_call(cast(Any, item))
        next(hook)
        outcome = Outcome()

        with pytest.raises(StopIteration):
            hook.send(outcome)

    assert recorded[0].code == "crash"
    assert isinstance(outcome.exception, pytest.fail.Exception)
    assert str(outcome.exception) == "Kensa engine failure: stopped"


def test_engine_client_forces_stuck_process_shutdown() -> None:
    process = _Process(wait_timeouts=2)
    client = _raw_client(process)

    client.close()
    client.close()

    assert process.terminated
    assert process.killed
    assert process.stdin is not None
    assert process.stdin.closed


@pytest.mark.parametrize(
    ("process", "message", "code"),
    [
        (_Process(stdin=None), "pipes are unavailable", "transport"),
        (
            _Process(stdin=_Stream(write_error=BrokenPipeError("closed"))),
            "Could not write",
            "transport",
        ),
        (_Process(), "stopped before responding", "crash"),
        (_Process("not-json\n"), "malformed JSON", "protocol"),
        (_Process(_response({}, request_id="wrong")), "mismatched response", "protocol"),
        (_Process(_response([], request_id="1")), "response is not an object", "protocol"),
    ],
)
def test_engine_client_rejects_transport_failures(
    process: _Process,
    message: str,
    code: str,
) -> None:
    client = _raw_client(process)
    if message == "pipes are unavailable":
        process.stdin = None

    with pytest.raises(KensaEngineError, match=message) as exc_info:
        client._request({"type": "test"})

    assert exc_info.value.code == code


@pytest.mark.parametrize(
    ("failure", "expected_message", "expected_code", "expected_details"),
    [
        (
            {"message": "rejected", "code": "invalid_message", "details": {"field": "x"}},
            "rejected",
            "invalid_message",
            {"field": "x"},
        ),
        (None, "request failed", "protocol", {}),
        ({"message": 1, "code": 2, "details": []}, "request failed", "protocol", {}),
    ],
)
def test_engine_client_preserves_structured_failures(
    failure: Any,
    expected_message: str,
    expected_code: str,
    expected_details: dict[str, Any],
) -> None:
    client = _raw_client(_Process(_response(failure, ok=False)))

    with pytest.raises(KensaEngineError, match=expected_message) as exc_info:
        client._request({"type": "test"})

    assert exc_info.value.code == expected_code
    assert exc_info.value.details == expected_details


def test_closed_engine_client_rejects_requests() -> None:
    client = _raw_client(_Process())
    client._closed = True
    with pytest.raises(KensaEngineError, match="client is closed"):
        client._request({"type": "test"})


def test_runtime_rejects_non_json_engine_input_and_reuses_verdict() -> None:
    class StubEngine:
        def __init__(self) -> None:
            self.completed = 0

        def start_case(self, evaluation_id: str, case: Any) -> None:
            del evaluation_id, case

        def complete_case(self, *_: Any, **__: Any) -> str:
            self.completed += 1
            return "pass"

    engine = StubEngine()
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test",
        group_id="test",
        case_id="test",
        no_judge=False,
        engine=cast(Any, engine),
    )
    bad_case = kensa_case(id="bad", input=object())
    with pytest.raises(KensaCaseError, match="input must be JSON-serializable"):
        runtime.run_case(bad_case, lambda: None)

    good_runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test-good",
        group_id="test-good",
        case_id="test-good",
        no_judge=False,
        engine=cast(Any, engine),
    )
    good_runtime.run_case(kensa_case(id="good", input=None), lambda: None)
    assert good_runtime.finalize_engine("pass", None) == "pass"
    assert good_runtime.finalize_engine("error", None) == "pass"
    assert engine.completed == 1
