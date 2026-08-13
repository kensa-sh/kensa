from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
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
    EngineCompletion,
    EngineConversationAction,
    EngineConversationResult,
    KensaEngineError,
    _check_outcome,
    _conversation_step,
    _engine_command,
    _engine_executable,
    _wire_json_value,
)
from kensa.errors import KensaCaseError, TrialFailure
from kensa.pytest_plugin import _classify_exception
from kensa.runtime import KensaTrial, KensaTrialRuntime, _engine_trace


class _Stream:
    def __init__(
        self,
        *lines: str,
        write_error: BaseException | None = None,
        read_error: BaseException | None = None,
        close_error: OSError | None = None,
    ) -> None:
        self.lines = list(lines)
        self.write_error = write_error
        self.read_error = read_error
        self.close_error = close_error
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
        if self.read_error is not None:
            raise self.read_error
        return self.lines.pop(0) if self.lines else ""

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _Process:
    def __init__(
        self,
        *responses: str,
        stdin: _Stream | None = None,
        wait_timeouts: int = 0,
        wait_errors: int = 0,
        terminate_error: OSError | None = None,
        kill_error: OSError | None = None,
    ) -> None:
        self.stdin: _Stream | None = stdin if stdin is not None else _Stream()
        self.stdout = _Stream(*responses)
        self.wait_timeouts = wait_timeouts
        self.wait_errors = wait_errors
        self.terminate_error = terminate_error
        self.kill_error = kill_error
        self.wait_calls = 0
        self.terminated = False
        self.killed = False

    def wait(self, timeout: int | None = None) -> int:
        del timeout
        self.wait_calls += 1
        if self.wait_errors:
            self.wait_errors -= 1
            raise OSError("wait failed")
        if self.wait_timeouts:
            self.wait_timeouts -= 1
            raise subprocess.TimeoutExpired("engine", 5)
        return 0

    def terminate(self) -> None:
        self.terminated = True
        if self.terminate_error is not None:
            raise self.terminate_error

    def kill(self) -> None:
        self.killed = True
        if self.kill_error is not None:
            raise self.kill_error

    def poll(self) -> int:
        return 7


def _raw_client(process: _Process) -> Any:
    client = object.__new__(EngineClient)
    client._process = process
    client._lock = Lock()
    client._request_number = 0
    client._closed = False
    client._closing = False
    client._handshake_complete = False
    client._active_evaluations = set()
    return client


def _trial_payload(*, nodeid: str = "node", status: str = "pass") -> dict[str, Any]:
    return {
        "nodeid": nodeid,
        "group_id": nodeid,
        "case_id": nodeid,
        "trial_index": 1,
        "configured_trials": 1,
        "status": status,
        "case": {"id": nodeid},
        "output": None,
        "failure": None,
        "duration_ms": 0.0,
        "trace": {},
        "judges": [],
        "active_operation": None,
        "smoke": False,
    }


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
        assert verdict == EngineCompletion(
            verdict="pass",
            failure=None,
            checks=({"id": "pytest", "outcome": "satisfied", "failure": None},),
            judges=(),
        )

        client.start_case(
            "cancelled",
            {"id": "cancelled", "input": None, "metadata": {}},
        )
        client.cancel_case("cancelled", "test stopped")


def test_engine_client_runs_conversation_lifecycle() -> None:
    with EngineClient() as client:
        action = client.start_conversation(
            "conversation",
            {
                "messages": [{"role": "system", "content": "private"}],
                "mode": "direct",
                "max_agent_responses": None,
                "starts_with": "agent",
            },
        )
        assert action == EngineConversationAction(
            source="agent",
            messages=({"role": "system", "content": "private"},),
            response_index=1,
            agent_responses=0,
            accepted_messages=({"role": "system", "content": "private"},),
            accepted_output=None,
            accepted_output_recorded=False,
        )

        result = client.observe_conversation(
            "conversation",
            {
                "source": "agent",
                "content": "done",
                "output": {"ok": True},
                "output_recorded": True,
                "termination_reason": None,
            },
        )
        assert result == EngineConversationResult(
            messages=(
                {"role": "system", "content": "private"},
                {"role": "assistant", "content": "done"},
            ),
            output={"ok": True},
            output_recorded=True,
            termination_source="engine",
            termination_reason="direct",
        )


def test_engine_client_rejects_terminal_conversation_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(EngineClient)
    monkeypatch.setattr(
        client,
        "_request",
        lambda request: {
            "type": "conversation_result",
            "conversation_id": request["conversation_id"],
            "result": {
                "phase": "complete",
                "messages": [],
                "output": None,
                "output_recorded": False,
                "termination": {"source": "engine", "reason": "direct"},
            },
        },
    )

    with pytest.raises(KensaEngineError, match="before a response"):
        client.start_conversation("conversation", {})


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"type": "conversation_action", "conversation_id": "other"}, "mismatched"),
        ({"type": "unknown", "conversation_id": "conversation"}, "invalid conversation response"),
        (
            {"type": "conversation_action", "conversation_id": "conversation", "action": None},
            "invalid conversation action",
        ),
        (
            {
                "type": "conversation_action",
                "conversation_id": "conversation",
                "action": {
                    "source": "other",
                    "messages": [],
                    "response_index": True,
                    "agent_responses": -1,
                    "accepted": {},
                },
            },
            "invalid conversation action",
        ),
        (
            {
                "type": "conversation_action",
                "conversation_id": "conversation",
                "action": {
                    "source": "agent",
                    "messages": [],
                    "response_index": 1,
                    "agent_responses": 0,
                    "accepted": {
                        "messages": [],
                        "output": None,
                        "output_recorded": "yes",
                    },
                },
            },
            "invalid accepted",
        ),
        (
            {
                "type": "conversation_action",
                "conversation_id": "conversation",
                "action": {
                    "source": "agent",
                    "messages": [],
                    "response_index": 1,
                    "agent_responses": 0,
                    "accepted": {
                        "messages": [],
                        "output": "impossible",
                        "output_recorded": False,
                    },
                },
            },
            "contradictory accepted",
        ),
        (
            {
                "type": "conversation_action",
                "conversation_id": "conversation",
                "action": {
                    "source": "agent",
                    "messages": "invalid",
                    "response_index": 1,
                    "agent_responses": 0,
                    "accepted": {
                        "messages": [],
                        "output": None,
                        "output_recorded": False,
                    },
                },
            },
            "invalid action messages",
        ),
        (
            {
                "type": "conversation_action",
                "conversation_id": "conversation",
                "action": {
                    "source": "agent",
                    "messages": [{"role": "unknown", "content": "invalid"}],
                    "response_index": 1,
                    "agent_responses": 0,
                    "accepted": {
                        "messages": [],
                        "output": None,
                        "output_recorded": False,
                    },
                },
            },
            "invalid action messages",
        ),
        (
            {
                "type": "conversation_action",
                "conversation_id": "conversation",
                "action": {
                    "source": "agent",
                    "messages": [],
                    "response_index": 1,
                    "agent_responses": 0,
                    "accepted": {
                        "messages": [],
                        "output": object(),
                        "output_recorded": True,
                    },
                },
            },
            "invalid accepted conversation output",
        ),
        (
            {
                "type": "conversation_action",
                "conversation_id": "conversation",
                "action": {
                    "source": "agent",
                    "messages": [],
                    "response_index": 1,
                    "agent_responses": 0,
                    "accepted": {
                        "messages": [],
                        "output": (1,),
                        "output_recorded": True,
                    },
                },
            },
            "contradictory accepted conversation output",
        ),
        (
            {"type": "conversation_result", "conversation_id": "conversation", "result": None},
            "invalid conversation result",
        ),
        (
            {
                "type": "conversation_result",
                "conversation_id": "conversation",
                "result": {
                    "phase": "complete",
                    "messages": [],
                    "output": None,
                    "output_recorded": "yes",
                    "termination": None,
                },
            },
            "invalid conversation result",
        ),
        (
            {
                "type": "conversation_result",
                "conversation_id": "conversation",
                "result": {
                    "phase": "complete",
                    "messages": [],
                    "output": None,
                    "output_recorded": False,
                    "termination": {"source": "engine", "reason": " "},
                },
            },
            "invalid conversation termination",
        ),
        (
            {
                "type": "conversation_result",
                "conversation_id": "conversation",
                "result": {
                    "phase": "complete",
                    "messages": [],
                    "output": "impossible",
                    "output_recorded": False,
                    "termination": {"source": "engine", "reason": "direct"},
                },
            },
            "contradictory conversation output",
        ),
    ],
)
def test_engine_client_rejects_invalid_conversation_responses(
    response: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(KensaEngineError, match=message):
        _conversation_step(response, "conversation")


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


def test_ordinary_pytest_session_does_not_start_engine(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KENSA_ENGINE_COMMAND", str(tmp_path / "missing-engine"))
    pytester.makepyfile(
        test_plain="""
def test_plain():
    pass
"""
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.OK


def test_engine_crash_during_finalize_records_infrastructure_failure(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = tmp_path / "crashing-engine.py"
    engine.write_text(
        """
import json
import os
import sys
from kensa.engine import EngineClient, _engine_command

configured_engine = os.environ.pop("KENSA_ENGINE_COMMAND", None)
real_engine = _engine_command()
if configured_engine is not None:
    os.environ["KENSA_ENGINE_COMMAND"] = configured_engine

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
    elif request["type"] == "build_run":
        with EngineClient(real_engine) as engine:
            result = engine.build_run(
                run_id=request["run_id"],
                complete=request["complete"],
                interruption=request["interruption"],
                trials=request["trials"],
            )
        response = {
            "type": "run_result",
            "result": result,
        }
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
    result.stdout.fnmatch_lines(
        ["*Kensa engine finalization failed: Kensa engine stopped before responding*"]
    )
    result_files = list((artifact_dir / "results").glob("*.json"))
    assert len(result_files) == 1
    payload = json.loads(result_files[0].read_text(encoding="utf-8"))
    assert payload["trials"][0]["status"] == "error"
    assert payload["trials"][0]["failure"]["category"] == "infrastructure"
    assert payload["trials"][0]["failure"]["kind"] == "crash"


def test_engine_finalization_failure_cancels_active_evaluation(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requests_path = tmp_path / "requests.jsonl"
    engine = tmp_path / "rejecting-engine.py"
    engine.write_text(
        f"""
import json
import os
import sys
from pathlib import Path
from kensa.engine import EngineClient, _engine_command

configured_engine = os.environ.pop("KENSA_ENGINE_COMMAND", None)
real_engine = _engine_command()
if configured_engine is not None:
    os.environ["KENSA_ENGINE_COMMAND"] = configured_engine

requests_path = Path({str(requests_path)!r})
for line in sys.stdin:
    envelope = json.loads(line)
    request = envelope["request"]
    with requests_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(request) + "\\n")
    if request["type"] == "handshake":
        response = {{
            "type": "handshake",
            "protocol_version": "kensa.engine.v1",
            "engine_version": "test",
        }}
    elif request["type"] == "start_case":
        response = {{"type": "action", "action": "invoke_agent", "case_id": "one"}}
    elif request["type"] == "observe":
        response = {{"type": "action", "action": "wrong"}}
    elif request["type"] == "cancel":
        response = {{
            "type": "result",
            "evaluation": {{
                "phase": "cancelled",
                "verdict": "error",
                "failure": {{
                    "category": "harness",
                    "kind": "cancelled",
                    "message": request["reason"],
                    "evidence": {{}},
                }},
            }},
        }}
    elif request["type"] == "build_run":
        with EngineClient(real_engine) as engine:
            result = engine.build_run(
                run_id=request["run_id"],
                complete=request["complete"],
                interruption=request["interruption"],
                trials=request["trials"],
            )
        response = {{
            "type": "run_result",
            "result": result,
        }}
    else:
        response = {{"type": "reset", "released": 0}}
    print(json.dumps({{"id": envelope["id"], "ok": True, "response": response}}), flush=True)
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
    requests = [json.loads(line) for line in requests_path.read_text(encoding="utf-8").splitlines()]
    request_types = [request["type"] for request in requests]
    assert request_types[:2] == ["handshake", "start_case"]
    assert request_types.index("observe") < request_types.index("cancel")
    assert request_types.index("cancel") < request_types.index("reset")
    cancellation = next(request for request in requests if request["type"] == "cancel")
    assert "ended before engine finalization" in cancellation["reason"]
    result_path = next((artifact_dir / "results").glob("*.json"))
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["trials"][0]["failure"]["category"] == "infrastructure"


def test_missing_default_engine_is_a_usage_error(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class MissingEngine:
        def __init__(self) -> None:
            raise KensaEngineError("missing", code="startup")

    monkeypatch.delenv("KENSA_ENGINE_COMMAND", raising=False)
    monkeypatch.setattr(pytest_plugin, "EngineClient", MissingEngine)
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import ConversationResponse, kensa_case

class Agent:
    def respond(self, messages):
        return ConversationResponse(content="hello")

@pytest.mark.kensa
@pytest.mark.parametrize("case", [kensa_case(id="fallback", input=None)])
def test_agent(case):
    assert case.run(Agent()).output == "hello"
"""
    )

    result = pytester.runpytest(
        "-q",
        "--kensa-write-artifacts",
        f"--kensa-artifact-dir={tmp_path / 'artifacts'}",
    )

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*Kensa engine startup failed: missing*"])


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
        "agent_runs": [
            {"event": {"timestamp": 1_786_430_000_000_000_001}},
            {"event": {"timestamp": 1}},
        ],
    }

    wire = _engine_trace(original)

    assert wire["spans"][0]["start_time_unix_nano"] == "1786430000000000000"
    assert wire["spans"][0]["end_time_unix_nano"] is None
    assert wire["agent_runs"][0]["event"]["timestamp"] == "1786430000000000001"
    assert wire["agent_runs"][1]["event"]["timestamp"] == "1"
    assert original_span["start_time_unix_nano"] == 1_786_430_000_000_000_000
    assert _engine_trace({"spans": None}) == {"spans": None}


def test_engine_build_run_preserves_nanosecond_timestamps_as_decimal_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = EngineClient()
    canonical_engine = EngineClient()
    requests: list[dict[str, Any]] = []

    def request(payload: dict[str, Any]) -> dict[str, Any]:
        requests.append(payload)
        return {
            "type": "run_result",
            "result": canonical_engine.build_run(
                run_id=payload["run_id"],
                complete=payload["complete"],
                interruption=payload["interruption"],
                trials=payload["trials"],
            ),
        }

    monkeypatch.setattr(client, "_request", request)

    result = client.build_run(
        run_id="run",
        complete=True,
        interruption=None,
        trials=[
            {
                **_trial_payload(),
                "trace": {
                    "spans": [
                        {
                            "start_time_unix_nano": 1_786_430_000_000_000_000,
                            "end_time_unix_nano": 1_786_430_000_000_000_001,
                        }
                    ]
                },
            }
        ],
    )

    assert result["schema_version"] == "kensa.result.v1"
    assert result["run_id"] == "run"
    assert result["complete"] is True
    assert result["interruption"] is None
    assert result["trials"] == requests[0]["trials"]
    spans = requests[0]["trials"][0]["trace"]["spans"]
    assert spans[0]["start_time_unix_nano"] == "1786430000000000000"
    assert spans[0]["end_time_unix_nano"] == "1786430000000000001"
    canonical_engine.close()
    client.close()


def test_engine_client_rejects_invalid_run_result_and_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = EngineClient()
    monkeypatch.setattr(client, "_request", lambda payload: {"type": "wrong"})
    with pytest.raises(KensaEngineError, match="invalid run result"):
        client.build_run(
            run_id="run",
            complete=True,
            interruption=None,
            trials=[],
        )

    with EngineClient() as canonical_engine:
        contradictory = canonical_engine.build_run(
            run_id="other",
            complete=True,
            interruption=None,
            trials=[],
        )
    monkeypatch.setattr(
        client,
        "_request",
        lambda payload: {"type": "run_result", "result": contradictory},
    )
    with pytest.raises(KensaEngineError, match="contradictory run result"):
        client.build_run(
            run_id="run",
            complete=True,
            interruption=None,
            trials=[],
        )

    monkeypatch.setattr(
        client,
        "_request_locked",
        lambda payload: {"type": "reset", "released": True},
    )
    with pytest.raises(KensaEngineError, match="invalid reset"):
        client.reset()
    client.close()


@pytest.mark.parametrize("field", ["complete", "interruption", "trials"])
def test_engine_client_rejects_type_sensitive_run_result_contradictions(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    requested_trial = _trial_payload(nodeid="requested")
    requested_complete = field != "interruption"
    requested_interruption = (
        {"kind": "crash", "message": "requested"} if field == "interruption" else None
    )
    with EngineClient() as canonical_engine:
        if field == "complete":
            response_result = canonical_engine.build_run(
                run_id="run",
                complete=False,
                interruption=None,
                trials=[requested_trial],
            )
        elif field == "interruption":
            response_result = canonical_engine.build_run(
                run_id="run",
                complete=False,
                interruption={"kind": "crash", "message": "response"},
                trials=[requested_trial],
            )
        else:
            response_result = canonical_engine.build_run(
                run_id="run",
                complete=True,
                interruption=None,
                trials=[_trial_payload(nodeid="response")],
            )
    client = EngineClient()
    monkeypatch.setattr(
        client,
        "_request",
        lambda payload: {"type": "run_result", "result": response_result},
    )

    with pytest.raises(KensaEngineError, match="contradictory run result"):
        client.build_run(
            run_id="run",
            complete=requested_complete,
            interruption=requested_interruption,
            trials=[requested_trial],
        )
    client.close()


@pytest.mark.parametrize("field", ["schema_version", "aggregates", "summary"])
def test_engine_client_requires_complete_run_result_schema(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    with EngineClient() as canonical_engine:
        response_result = canonical_engine.build_run(
            run_id="run",
            complete=True,
            interruption=None,
            trials=[],
        )
    del response_result[field]
    client = EngineClient()
    monkeypatch.setattr(
        client,
        "_request",
        lambda payload: {"type": "run_result", "result": response_result},
    )

    with pytest.raises(KensaEngineError, match="invalid run result"):
        client.build_run(
            run_id="run",
            complete=True,
            interruption=None,
            trials=[],
        )
    client.close()


def test_engine_client_rejects_unknown_aggregate_trial_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = _trial_payload()
    with EngineClient() as canonical_engine:
        response_result = canonical_engine.build_run(
            run_id="run",
            complete=True,
            interruption=None,
            trials=[trial],
        )
    response_result["aggregates"][0]["trials"][0]["nodeid"] = "ghost"
    client = EngineClient()
    monkeypatch.setattr(
        client,
        "_request",
        lambda payload: {"type": "run_result", "result": response_result},
    )

    with pytest.raises(KensaEngineError, match="invalid run result"):
        client.build_run(
            run_id="run",
            complete=True,
            interruption=None,
            trials=[trial],
        )
    client.close()


def test_engine_client_rejects_boolean_trial_values_as_integer_echoes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_trial = _trial_payload()
    requested_trial["output"] = {"value": 1}
    response_trial = _trial_payload()
    response_trial["output"] = {"value": True}
    with EngineClient() as canonical_engine:
        response_result = canonical_engine.build_run(
            run_id="run",
            complete=True,
            interruption=None,
            trials=[response_trial],
        )
    client = EngineClient()
    monkeypatch.setattr(
        client,
        "_request",
        lambda payload: {"type": "run_result", "result": response_result},
    )

    with pytest.raises(KensaEngineError, match="contradictory run result"):
        client.build_run(
            run_id="run",
            complete=True,
            interruption=None,
            trials=[requested_trial],
        )
    client.close()


def test_engine_client_normalizes_trace_views(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = EngineClient()
    requests: list[dict[str, Any]] = []

    def request(payload: dict[str, Any]) -> dict[str, Any]:
        requests.append(payload)
        return {"type": "trace_views", "traces": payload["traces"]}

    monkeypatch.setattr(client, "_request", request)
    normalized = client.normalize_trace_views(
        [{"id": "trace", "started_at_unix_nano": 1_786_430_000_000_000_000}]
    )

    assert normalized == [{"id": "trace", "started_at_unix_nano": "1786430000000000000"}]
    assert requests[0]["type"] == "normalize_traces"
    monkeypatch.setattr(
        client,
        "_request",
        lambda payload: {"type": "trace_views", "traces": [None]},
    )
    with pytest.raises(KensaEngineError, match="invalid trace views"):
        client.normalize_trace_views([])
    monkeypatch.setattr(
        client,
        "_request",
        lambda payload: {"type": "trace_views", "traces": [{"id": "different"}]},
    )
    with pytest.raises(KensaEngineError, match="contradictory identities"):
        client.normalize_trace_views([{"id": "requested"}])
    client.close()


def test_wire_json_value_preserves_types_and_rejects_lossy_values() -> None:
    assert _wire_json_value(
        {
            "values": [True, None, 1.5, "text", 9_007_199_254_740_991],
            1: (1,),
        }
    ) == {
        "values": [True, None, 1.5, "text", 9_007_199_254_740_991],
        "1": [1],
    }
    with pytest.raises(ValueError, match="interoperable JSON range"):
        _wire_json_value({"value": 9_007_199_254_740_992})
    with pytest.raises(TypeError, match="not JSON-serializable"):
        _wire_json_value({"bytes": b"opaque"})
    with pytest.raises(ValueError, match="non-finite"):
        _wire_json_value({"value": float("nan")})
    with pytest.raises(ValueError, match="duplicate keys"):
        _wire_json_value({1: "integer", "1": "string"})


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
    assert process.wait_calls == 1


@pytest.mark.parametrize(
    ("response", "message", "code"),
    [
        ("not-json\n", "malformed JSON", "protocol"),
        (
            _response(
                {
                    "message": "unsupported protocol",
                    "code": "version_mismatch",
                },
                ok=False,
            ),
            "unsupported protocol",
            "version_mismatch",
        ),
    ],
)
def test_engine_client_reaps_failed_handshakes(
    monkeypatch: pytest.MonkeyPatch,
    response: str,
    message: str,
    code: str,
) -> None:
    process = _Process(response)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)

    with pytest.raises(KensaEngineError, match=message) as exc_info:
        EngineClient(("engine",))

    assert exc_info.value.code == code
    assert process.stdin is not None
    assert process.stdin.closed
    assert process.wait_calls == 1


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
                    "evaluation": {"phase": "cancelled", "verdict": "pass"},
                },
            ],
            "non-terminal result",
        ),
        (
            "complete",
            [
                {"type": "action", "action": "evaluate_check"},
                {
                    "type": "result",
                    "evaluation": {
                        "phase": "complete",
                        "verdict": "fail",
                        "failure": "invalid",
                    },
                },
            ],
            "invalid failure",
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
            "failure provenance",
        ),
        (
            "complete",
            [
                {"type": "action", "action": "evaluate_check"},
                {
                    "type": "result",
                    "evaluation": {
                        "phase": "complete",
                        "verdict": "pass",
                        "checks": None,
                        "judges": [],
                    },
                },
            ],
            "invalid checks",
        ),
        (
            "complete",
            [
                {"type": "action", "action": "evaluate_check"},
                {
                    "type": "result",
                    "evaluation": {
                        "phase": "complete",
                        "verdict": "pass",
                        "checks": [{}],
                        "judges": [None],
                    },
                },
            ],
            "invalid judges",
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
    monkeypatch.setattr(client, "_request_locked", lambda request, **kwargs: next(iterator))

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


def test_engine_client_uses_authoritative_terminal_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = EngineClient()
    responses = iter(
        [
            {"type": "action", "action": "evaluate_check"},
            {
                "type": "result",
                "evaluation": {
                    "phase": "complete",
                    "verdict": "pass",
                    "checks": [{"id": "pytest", "outcome": "satisfied", "failure": None}],
                    "judges": [],
                },
            },
        ]
    )
    monkeypatch.setattr(client, "_request_locked", lambda request, **kwargs: next(responses))

    completion = client.complete_case(
        "eval",
        observation={},
        status="fail",
        failure={"category": "agent"},
    )
    assert completion == EngineCompletion(
        verdict="pass",
        failure=None,
        checks=({"id": "pytest", "outcome": "satisfied", "failure": None},),
        judges=(),
    )
    with pytest.raises(KensaEngineError, match="Unknown check status"):
        _check_outcome("unknown")
    client.close()


def test_passing_test_engine_finalize_failure_is_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime:
        def finalize_engine(self, status: str, failure: Any) -> tuple[str, Any]:
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
    assert str(outcome.exception) == "Kensa engine finalization failed: stopped"


def test_engine_nonpass_verdict_fails_passing_pytest_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = TrialFailure(
        category="judge",
        kind="threshold",
        message="score was below threshold",
    )

    class Runtime:
        nodeid = "test_eval"

        def finalize_engine(self, status: str, original: Any) -> tuple[str, TrialFailure]:
            del status, original
            return "fail", failure

        def metadata(self, **values: Any) -> Any:
            return values

    class Outcome:
        excinfo = None

        def __init__(self) -> None:
            self.exception: BaseException | None = None

        def force_exception(self, exception: BaseException) -> None:
            self.exception = exception

    state = SimpleNamespace(set_active_phase=lambda *_: None)
    item = SimpleNamespace(config=object(), nodeid="test_eval")
    recorded: list[Any] = []
    with monkeypatch.context() as patcher:
        patcher.setattr(pytest_plugin, "_runtime_for_item", lambda _: Runtime())
        patcher.setattr(pytest_plugin, "_state", lambda _: state)
        patcher.setattr(
            pytest_plugin, "_record_trial", lambda _config, value: recorded.append(value)
        )
        hook = pytest_plugin.pytest_runtest_call(cast(Any, item))
        next(hook)
        outcome = Outcome()
        with pytest.raises(StopIteration):
            hook.send(outcome)

    assert recorded[0]["status"] == "fail"
    assert recorded[0]["failure"] == failure
    assert isinstance(outcome.exception, pytest.fail.Exception)
    assert str(outcome.exception) == failure.message


def test_engine_pass_on_raising_pytest_item_marks_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime:
        nodeid = "test_eval"

        def finalize_engine(self, status: str, failure: Any) -> tuple[str, None]:
            del status, failure
            return "pass", None

        def metadata(self, **values: Any) -> Any:
            return values

    class Outcome:
        excinfo = (None, AssertionError("failed locally"), None)

        def force_exception(self, exception: BaseException) -> None:
            raise AssertionError(f"unexpected replacement: {exception}")

    interruptions: list[tuple[Any, ...]] = []
    state = SimpleNamespace(
        set_active_phase=lambda *_: None,
        mark_incomplete=lambda *args, **kwargs: interruptions.append((*args, kwargs)),
    )
    item = SimpleNamespace(config=object(), nodeid="test_eval")
    recorded: list[Any] = []
    with monkeypatch.context() as patcher:
        patcher.setattr(pytest_plugin, "_runtime_for_item", lambda _: Runtime())
        patcher.setattr(pytest_plugin, "_state", lambda _: state)
        patcher.setattr(
            pytest_plugin, "_record_trial", lambda _config, value: recorded.append(value)
        )
        hook = pytest_plugin.pytest_runtest_call(cast(Any, item))
        next(hook)
        with pytest.raises(StopIteration):
            hook.send(Outcome())

    assert recorded[0]["status"] == "pass"
    assert interruptions == [
        (
            "verdict_mismatch",
            "Kensa engine passed a trial whose pytest call raised",
            {"nodeid": "test_eval"},
        )
    ]


def test_engine_client_forces_stuck_process_shutdown() -> None:
    process = _Process(wait_timeouts=2)
    client = _raw_client(process)

    client.close()
    client.close()

    assert process.terminated
    assert process.killed
    assert process.stdin is not None
    assert process.stdin.closed


def test_engine_client_shutdown_suppresses_process_errors() -> None:
    process = _Process(
        stdin=_Stream(close_error=OSError("close failed")),
        wait_errors=3,
        terminate_error=OSError("terminate failed"),
        kill_error=OSError("kill failed"),
    )
    client = _raw_client(process)

    client.close()

    assert client._closed
    assert process.terminated
    assert process.killed
    assert process.wait_calls == 3


def test_engine_client_shutdown_handles_missing_stdin_and_termination() -> None:
    process = _Process(wait_timeouts=1)
    process.stdin = None
    client = _raw_client(process)

    client.close()

    assert client._closed
    assert process.terminated
    assert not process.killed
    assert process.wait_calls == 2


def test_engine_client_close_cancels_active_evaluations() -> None:
    process = _Process(
        _response(
            {"type": "result", "evaluation": {"phase": "cancelled"}},
            request_id="1",
        ),
        _response({"type": "reset", "released": 0}, request_id="2"),
    )
    client = _raw_client(process)
    client._handshake_complete = True
    client._active_evaluations.add("active")

    client.close()

    assert process.stdin is not None
    requests = [json.loads(line)["request"] for line in process.stdin.writes]
    assert requests == [
        {
            "type": "cancel",
            "evaluation_id": "active",
            "reason": "Python engine client closed",
        },
        {"type": "reset"},
    ]
    assert client._active_evaluations == set()


def test_engine_client_close_suppresses_invalid_cancellation() -> None:
    process = _Process(
        _response({"type": "wrong"}, request_id="1"),
        _response({"type": "reset", "released": 1}, request_id="2"),
    )
    client = _raw_client(process)
    client._handshake_complete = True
    client._active_evaluations.add("active")

    client.close()

    assert client._closed
    assert client._active_evaluations == set()


def test_engine_client_close_reports_reset_failure() -> None:
    client = _raw_client(_Process())
    client._handshake_complete = True

    shutdown_error = client.close()

    assert shutdown_error is not None
    assert shutdown_error.code == "crash"
    assert client._closed


def test_engine_client_close_reaps_after_unexpected_handshake_error() -> None:
    process = _Process()
    process.stdout = _Stream(read_error=RuntimeError("unexpected"))
    client = _raw_client(process)
    client._handshake_complete = True

    client.close()

    assert client._closed
    assert process.stdin is not None
    assert process.stdin.closed
    assert process.wait_calls == 1


def test_engine_client_close_reaps_before_reraising_interrupt() -> None:
    process = _Process()
    process.stdout = _Stream(read_error=KeyboardInterrupt())
    client = _raw_client(process)
    client._handshake_complete = True

    with pytest.raises(KeyboardInterrupt):
        client.close()

    assert client._closed
    assert process.stdin is not None
    assert process.stdin.closed
    assert process.wait_calls == 1


def test_engine_client_close_bounds_unresponsive_engine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = tmp_path / "unresponsive-engine.py"
    engine.write_text(
        """
import json
import sys
import time

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
        time.sleep(60)
        continue
    print(json.dumps({"id": envelope["id"], "ok": True, "response": response}), flush=True)
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("kensa.engine._RESPONSE_TIMEOUT_S", 0.05)
    client = EngineClient((sys.executable, str(engine)))
    client.start_case("active", {"id": "one", "input": None, "metadata": {}})

    started = time.monotonic()
    client.close()

    assert time.monotonic() - started < 2
    assert client._closed
    assert client._process.poll() is not None


def test_engine_client_cancel_all_attempts_every_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _raw_client(_Process())
    client._active_evaluations = {"first", "second"}
    attempted: list[str] = []

    def cancel(evaluation_id: str, reason: str) -> None:
        del reason
        attempted.append(evaluation_id)
        if evaluation_id == "first":
            raise KensaEngineError("failed", code="transport")
        client._active_evaluations.discard(evaluation_id)

    monkeypatch.setattr(client, "_cancel_locked", cancel)

    with pytest.raises(KensaEngineError, match="failed") as exc_info:
        client.cancel_all("session ended")

    assert exc_info.value.code == "transport"
    assert set(attempted) == {"first", "second"}


@pytest.mark.parametrize(
    ("process", "message", "code"),
    [
        (_Process(stdin=None), "pipes are unavailable", "transport"),
        (
            _Process(stdin=_Stream(write_error=BrokenPipeError("closed"))),
            "Could not write",
            "transport",
        ),
        (
            _Process(),
            "Could not read",
            "transport",
        ),
        (_Process(), "stopped before responding", "crash"),
        (_Process("not-json\n"), "malformed JSON", "protocol"),
        (_Process(_response({}, request_id="wrong")), "mismatched response", "protocol"),
        (_Process(_response([], request_id="1")), "response is not an object", "protocol"),
        (
            _Process(),
            "violates the JSON contract",
            "invalid_message",
        ),
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
    if message == "Could not read":
        process.stdout = _Stream(read_error=OSError("closed"))
    request = (
        {"type": "test", "unsafe": 9_007_199_254_740_992}
        if code == "invalid_message"
        else {"type": "test"}
    )

    with pytest.raises(KensaEngineError, match=message) as exc_info:
        client._request(request)

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


def test_session_engine_close_is_non_throwing_and_terminal() -> None:
    class StubEngine:
        def __init__(self) -> None:
            self.cancelled = False
            self.closed = False

        def cancel_all(self, reason: str) -> None:
            self.cancelled = "session" in reason
            raise KensaEngineError("cancel failed", code="transport")

        def close(self) -> None:
            self.closed = True
            raise OSError("close failed")

    config = SimpleNamespace(getoption=lambda name: None)
    state = pytest_plugin.KensaSessionState(cast(Any, config))
    engine = StubEngine()
    state._engine = cast(Any, engine)
    state._engine_resolved = True

    state.close_engine("pytest session closed")
    state.close_engine("pytest session closed again")

    assert engine.cancelled
    assert engine.closed
    assert state.complete is False
    assert state.interruption == {
        "kind": "engine_shutdown",
        "message": "cancellation failed: cancel failed; shutdown failed: close failed",
    }
    with pytest.raises(KensaEngineError, match="session is closed") as exc_info:
        _ = state.engine
    assert exc_info.value.code == "closed"
    with pytest.raises(KensaEngineError, match="session is closed"):
        _ = state.recovery_engine


def test_session_recovery_engine_failure_is_cached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    starts = 0

    class MissingEngine:
        def __init__(self) -> None:
            nonlocal starts
            starts += 1
            raise KensaEngineError("missing", code="startup")

    monkeypatch.setattr(pytest_plugin, "EngineClient", MissingEngine)
    config = SimpleNamespace(getoption=lambda name: str(tmp_path) if "artifact" in name else None)
    state = pytest_plugin.KensaSessionState(cast(Any, config))

    with pytest.raises(KensaEngineError, match="missing"):
        _ = state.recovery_engine
    with pytest.raises(KensaEngineError, match="missing"):
        _ = state.recovery_engine

    assert starts == 1


def test_artifact_write_does_not_spawn_repeated_engines_after_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recovery_starts = 0

    class FailingEngine:
        def build_run(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            raise KensaEngineError("failed", code="crash")

    class MissingRecovery:
        def __init__(self) -> None:
            nonlocal recovery_starts
            recovery_starts += 1
            raise KensaEngineError("missing", code="startup")

    monkeypatch.setattr(pytest_plugin, "EngineClient", MissingRecovery)
    config = SimpleNamespace(getoption=lambda name: str(tmp_path) if "artifact" in name else None)
    state = pytest_plugin.KensaSessionState(cast(Any, config))
    state._engine = cast(Any, FailingEngine())
    state._engine_resolved = True
    state.trials = [
        pytest_plugin.TrialMetadata(
            nodeid="node",
            group_id="group",
            case_id="case",
            trial_index=1,
            configured_trials=1,
            status="pass",
        )
    ]

    pytest_plugin._write_artifacts(state)
    pytest_plugin._write_artifacts(state)

    assert not state.result_path.exists()
    assert recovery_starts == 1


def test_session_engine_close_records_reported_shutdown_error() -> None:
    class StubEngine:
        def cancel_all(self, reason: str) -> None:
            del reason

        def close(self) -> KensaEngineError:
            return KensaEngineError("engine reported failure", code="shutdown")

    config = SimpleNamespace(getoption=lambda name: None)
    state = pytest_plugin.KensaSessionState(cast(Any, config))
    state._engine = cast(Any, StubEngine())
    state._engine_resolved = True

    state.close_engine()

    assert state.interruption == {
        "kind": "engine_shutdown",
        "message": "shutdown failed: engine reported failure",
    }


def test_runtime_rejects_non_json_engine_input_and_reuses_verdict() -> None:
    class StubEngine:
        def __init__(self) -> None:
            self.completed = 0

        def start_case(self, evaluation_id: str, case: Any) -> None:
            del evaluation_id, case

        def complete_case(self, *_: Any, **__: Any) -> EngineCompletion:
            self.completed += 1
            return EngineCompletion(verdict="pass", failure=None)

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
    assert good_runtime.finalize_engine("pass", None) == ("pass", None)
    assert good_runtime.finalize_engine("error", None) == ("pass", None)
    assert engine.completed == 1


def test_runtime_rejects_lossy_engine_evidence_as_case_failure() -> None:
    class StubEngine:
        completed = False

        def start_case(self, evaluation_id: str, case: Any) -> None:
            del evaluation_id, case

        def complete_case(self, *_: Any, **__: Any) -> EngineCompletion:
            self.completed = True
            return EngineCompletion(verdict="pass", failure=None)

    engine = StubEngine()
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test-evidence",
        group_id="test-evidence",
        case_id="test-evidence",
        no_judge=False,
        engine=cast(Any, engine),
    )
    runtime.run_case(
        kensa_case(id="unsafe", input=None),
        lambda: 9_007_199_254_740_992,
    )

    with pytest.raises(KensaCaseError, match=r"trial evidence.*interoperable JSON range"):
        runtime.finalize_engine("pass", None)

    assert not engine.completed

    input_runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test-input",
        group_id="test-input",
        case_id="test-input",
        no_judge=False,
        engine=cast(Any, engine),
    )
    with pytest.raises(KensaCaseError, match=r"input.*interoperable JSON range"):
        input_runtime.run_case(
            kensa_case(id="unsafe", input=9_007_199_254_740_992),
            lambda: None,
        )


def test_runtime_classifies_invalid_trace_evidence_as_case_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubEngine:
        completed = False

        def start_case(self, evaluation_id: str, case: Any) -> None:
            del evaluation_id, case

        def complete_case(self, *_: Any, **__: Any) -> EngineCompletion:
            self.completed = True
            return EngineCompletion(verdict="pass", failure=None)

    engine = StubEngine()
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test-trace",
        group_id="test-trace",
        case_id="test-trace",
        no_judge=False,
        engine=cast(Any, engine),
    )
    runtime.run_case(kensa_case(id="trace", input=None), lambda: "ok")
    monkeypatch.setattr(runtime.trace, "to_dict", lambda: {"unsafe": b"bytes"})

    with pytest.raises(KensaCaseError, match="trial evidence must be JSON-serializable"):
        runtime.finalize_engine("pass", None)

    assert not engine.completed


def test_runtime_uses_and_validates_engine_terminal_failure() -> None:
    terminal = {
        "category": "judge",
        "kind": "threshold",
        "message": "score was below threshold",
        "evidence": {"score": 0.2},
    }

    class StubEngine:
        def __init__(self, completion: EngineCompletion) -> None:
            self.completion = completion

        def start_case(self, evaluation_id: str, case: Any) -> None:
            del evaluation_id, case

        def complete_case(self, *_: Any, **__: Any) -> EngineCompletion:
            return self.completion

    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test-terminal",
        group_id="test-terminal",
        case_id="test-terminal",
        no_judge=False,
        engine=cast(Any, StubEngine(EngineCompletion(verdict="fail", failure=terminal))),
    )
    runtime.run_case(kensa_case(id="terminal", input=None), lambda: "ok")
    assert runtime.finalize_engine("pass", None) == (
        "fail",
        TrialFailure.model_validate(terminal),
    )

    invalid_runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test-invalid-terminal",
        group_id="test-invalid-terminal",
        case_id="test-invalid-terminal",
        no_judge=False,
        engine=cast(
            Any,
            StubEngine(
                EngineCompletion(
                    verdict="fail",
                    failure={"category": "judge", "kind": "threshold"},
                )
            ),
        ),
    )
    invalid_runtime.run_case(kensa_case(id="terminal", input=None), lambda: "ok")
    with pytest.raises(KensaEngineError, match="invalid terminal failure") as exc_info:
        invalid_runtime.finalize_engine("pass", None)
    assert exc_info.value.code == "protocol"
