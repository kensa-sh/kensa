from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from kensa.case import KensaCase, kensa_case
from kensa.errors import KensaEvalError
from kensa.target_client import TargetCommandSession


def _write_script(path: Path, source: str) -> Path:
    path.write_text(textwrap.dedent(source))
    return path


def _configure_target(root: Path, command: tuple[str, ...], *, timeout_s: float = 2.0) -> None:
    (root / "pyproject.toml").write_text(
        "[tool.kensa]\n"
        f"target_command = {json.dumps(list(command))}\n"
        f"target_timeout_s = {timeout_s}\n"
    )


def _host_script(path: Path) -> Path:
    return _write_script(
        path,
        """
        from __future__ import annotations

        import json
        import os
        import sys
        from pathlib import Path

        from kensa.conversation import ConversationResponse
        from kensa.target import (
            AgentEvent,
            AgentRunEvidence,
            ExecutionAttestation,
            StateObservation,
        )
        from kensa.target_command import TargetTurnResult, serve_target


        log_path = Path(sys.argv[1])


        class Agent:
            def __init__(self, case):
                self.case = case
                self.sentinel = f"{os.getpid()}-{case.id}"
                with log_path.open("a") as stream:
                    stream.write(json.dumps({
                        "event": "open",
                        "pid": os.getpid(),
                        "case": case.id,
                        "sentinel": self.sentinel,
                    }) + "\\n")

            def respond(self, messages):
                response = ConversationResponse(
                    content=f"reply:{len(messages)}",
                    output={"case": self.case.id, "messages": len(messages)},
                    termination_reason=self.case.row.get("termination_reason"),
                )
                if not self.case.row.get("evidence"):
                    return response
                complete = self.case.row.get("complete", True)
                evidence = AgentRunEvidence(
                    run_id=self.sentinel,
                    attestation=ExecutionAttestation(
                        revision="revision-1",
                        environment="sandbox",
                        effects="sandboxed",
                    ),
                    events=(
                        AgentEvent(
                            id=f"event-{self.sentinel}",
                            sequence=1,
                            kind="action",
                            name="configured-target",
                            status="completed",
                        ),
                    ),
                    trajectory_completeness="complete" if complete else "partial",
                    state=(
                        StateObservation(
                            name="session",
                            value={"sentinel": self.sentinel},
                            source="target",
                        ),
                    ),
                    state_completeness="complete" if complete else "unavailable",
                    incomplete_reason=None if complete else "target omitted some evidence",
                )
                return TargetTurnResult(response=response, evidence=evidence)

            def close(self):
                with log_path.open("a") as stream:
                    stream.write(json.dumps({
                        "event": "close",
                        "pid": os.getpid(),
                        "case": self.case.id,
                        "sentinel": self.sentinel,
                    }) + "\\n")


        raise SystemExit(serve_target(Agent))
        """,
    )


def _fault_script(path: Path) -> Path:
    return _write_script(
        path,
        r"""
        from __future__ import annotations

        import json
        import signal
        import sys
        import time


        behavior = sys.argv[1]


        def read():
            line = sys.stdin.buffer.readline()
            if not line:
                raise SystemExit(0)
            return json.loads(line)


        def write(payload):
            sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
            sys.stdout.flush()


        handshake = read()
        if behavior == "timeout_response":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            sys.stderr.write("target timeout diagnostic\n")
            sys.stderr.flush()
            time.sleep(60)
        if behavior == "crash_handshake":
            sys.stderr.write("target crash diagnostic\n")
            sys.stderr.flush()
            raise SystemExit(7)
        if behavior == "malformed":
            sys.stdout.write("{\n")
            sys.stdout.flush()
            time.sleep(60)
        if behavior == "nan":
            sys.stdout.write(
                '{"type":"handshake","request_id":"%s","version":NaN}\n'
                % handshake["request_id"]
            )
            sys.stdout.flush()
            time.sleep(60)
        if behavior == "non_utf8":
            sys.stdout.buffer.write(b"\xff\n")
            sys.stdout.buffer.flush()
            time.sleep(60)
        if behavior == "no_newline":
            sys.stdout.write("{")
            sys.stdout.flush()
            raise SystemExit(0)
        if behavior == "wrong_request_id":
            write({
                "type": "handshake",
                "request_id": "other",
                "version": "kensa.target.v1",
            })
            time.sleep(60)
        if behavior == "wrong_type":
            write({"type": "shutdown", "request_id": handshake["request_id"]})
            time.sleep(60)
        if behavior == "version_error":
            write({
                "type": "error",
                "request_id": handshake["request_id"],
                "code": "unsupported_version",
                "message": "unsupported",
                "fatal": True,
            })
            time.sleep(60)

        write({
            "type": "handshake",
            "request_id": handshake["request_id"],
            "version": "kensa.target.v1",
        })
        if behavior == "write_timeout":
            time.sleep(60)
        if behavior == "exit_before_open":
            raise SystemExit(7)

        opened = read()
        if behavior == "open_error":
            write({
                "type": "error",
                "request_id": opened["request_id"],
                "code": "target_open_failed",
                "message": "open failed",
                "fatal": True,
            })
            time.sleep(60)
        session_id = opened["session_id"]
        write({
            "type": "session_opened",
            "request_id": opened["request_id"],
            "session_id": "other" if behavior == "session_mismatch" else session_id,
        })
        if behavior == "session_mismatch":
            time.sleep(60)

        turn = read()
        if behavior == "crash_turn":
            sys.stderr.write("turn crash diagnostic\n")
            sys.stderr.flush()
            raise SystemExit(9)
        if behavior.startswith("output_"):
            number = {
                "output_nan": "NaN",
                "output_infinity": "Infinity",
                "output_overflow": "1e400",
            }[behavior]
            sys.stdout.write(
                '{"type":"turn","request_id":"%s","session_id":"%s",'
                '"response":{"content":"reply","output":{"value":%s},'
                '"termination_reason":null}}\n'
                % (turn["request_id"], session_id, number)
            )
            sys.stdout.flush()
            time.sleep(60)
        elif behavior == "turn_error":
            sys.stderr.write("responder private diagnostic\n")
            sys.stderr.flush()
            write({
                "type": "error",
                "request_id": turn["request_id"],
                "code": "target_turn_failed",
                "message": "turn failed",
                "fatal": True,
            })
        else:
            write({
                "type": "turn",
                "request_id": turn["request_id"],
                "session_id": session_id,
                "response": {
                    "content": "reply",
                    "output": {"ok": True},
                    "termination_reason": None,
                },
            })

        closed = read()
        if behavior == "cleanup_error":
            sys.stderr.write("cleanup private diagnostic\n")
            sys.stderr.flush()
            write({
                "type": "error",
                "request_id": closed["request_id"],
                "code": "target_close_failed",
                "message": "close failed",
                "fatal": True,
            })
            time.sleep(60)
        write({
            "type": "session_closed",
            "request_id": closed["request_id"],
            "session_id": session_id,
        })
        shutdown = read()
        write({"type": "shutdown", "request_id": shutdown["request_id"]})
        if behavior == "hang_exit":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(60)
        if behavior == "nonzero_exit":
            raise SystemExit(11)
        if behavior == "extra_output":
            time.sleep(0.02)
            write({"type": "shutdown", "request_id": "extra"})
        """,
    )


def _session(script: Path, behavior: str, *, timeout_s: float = 0.5) -> TargetCommandSession:
    return TargetCommandSession(
        (sys.executable, str(script), behavior),
        timeout_s=timeout_s,
        cwd=script.parent,
    )


def _assert_serialized_evidence(
    run: dict[str, Any],
    *,
    case_id: str,
    complete: bool,
) -> None:
    run_id = run["run_id"]
    assert run["schema_version"] == "kensa.agent_run.v1"
    assert run_id.endswith(case_id)
    assert run["attestation"] == {
        "revision": "revision-1",
        "environment": "sandbox",
        "effects": "sandboxed",
    }
    assert run["events"] == [
        {
            "id": f"event-{run_id}",
            "parent_id": None,
            "sequence": 1,
            "kind": "action",
            "name": "configured-target",
            "input": None,
            "output": None,
            "attributes": {},
            "status": "completed",
            "started_at_ns": None,
            "ended_at_ns": None,
        }
    ]
    assert run["state"] == [
        {
            "name": "session",
            "value": {"sentinel": run_id},
            "source": "target",
            "observed_at_ns": None,
        }
    ]
    expected_completeness = "complete" if complete else "partial"
    assert run["trajectory_completeness"] == expected_completeness
    assert run["state_completeness"] == ("complete" if complete else "unavailable")
    assert run["incomplete_reason"] == (None if complete else "target omitted some evidence")


def test_target_command_session_forwards_turn_and_closes_once(tmp_path: Path) -> None:
    log = tmp_path / "target.jsonl"
    script = _host_script(tmp_path / "target.py")
    session = TargetCommandSession(
        (sys.executable, str(script), str(log)),
        timeout_s=2,
        cwd=tmp_path,
    )
    case = kensa_case(id="direct", input="hello")

    session.open(case)
    response = session.respond(({"role": "user", "content": "hello"},))
    process = session._process
    session.close()
    session.close()

    assert response.content == "reply:1"
    assert response.output == {"case": "direct", "messages": 1}
    assert process is not None
    assert process.poll() == 0
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [event["event"] for event in events] == ["open", "close"]
    assert events[0]["pid"] == events[1]["pid"]
    with pytest.raises(RuntimeError, match="not active"):
        session.respond(())
    with pytest.raises(RuntimeError, match="opened only once"):
        session.open(case)


@pytest.mark.parametrize(
    ("behavior", "kind", "message", "last_completed"),
    [
        ("malformed", "target_protocol", "malformed protocol output", "none"),
        ("nan", "target_protocol", "malformed protocol output", "none"),
        ("non_utf8", "target_protocol", "non-UTF-8", "none"),
        ("no_newline", "target_protocol", "without a newline", "none"),
        ("wrong_request_id", "target_protocol", "request_id did not match", "none"),
        ("wrong_type", "target_protocol", "wrong frame type", "none"),
        ("version_error", "target_protocol", "unsupported_version", "handshake"),
        ("crash_handshake", "target_exit", "before completing handshake", "none"),
        ("timeout_response", "target_timeout", "timed out during response", "none"),
        ("open_error", "target_startup", "target_open_failed", "open_session"),
        ("session_mismatch", "target_protocol", "session_id did not match", "handshake"),
    ],
)
def test_startup_and_protocol_failures_abort_and_reap_process(
    tmp_path: Path,
    behavior: str,
    kind: str,
    message: str,
    last_completed: str,
) -> None:
    script = _fault_script(tmp_path / "fault.py")
    session = _session(script, behavior, timeout_s=0.1)

    with pytest.raises(KensaEvalError, match=message) as raised:
        session.open(kensa_case(id="case", input="hello"))

    assert raised.value.failure.category == "infrastructure"
    assert raised.value.failure.kind == kind
    assert raised.value.failure.evidence["last_completed_operation"] == last_completed
    assert session._process is not None
    assert session._process.poll() is not None
    if behavior in {"crash_handshake", "timeout_response"}:
        assert "diagnostic" in "\n".join(getattr(raised.value, "__notes__", []))


def test_invalid_command_and_request_are_classified_without_retry(tmp_path: Path) -> None:
    missing = TargetCommandSession(
        (str(tmp_path / "missing"),),
        timeout_s=0.1,
        cwd=tmp_path,
    )
    with pytest.raises(KensaEvalError) as startup:
        missing.open(kensa_case(id="case", input="hello"))
    assert startup.value.failure.kind == "target_startup"
    assert startup.value.failure.evidence["exception_type"] == "FileNotFoundError"
    assert missing._process is None

    script = _fault_script(tmp_path / "fault.py")
    invalid = _session(script, "success")
    case = KensaCase(id="invalid", row={"value": object()})
    with pytest.raises(KensaEvalError) as request:
        invalid.open(case)
    assert request.value.failure.category == "harness"
    assert request.value.failure.kind == "target_request"
    assert request.value.failure.evidence["operation"] == "open_session"
    assert invalid._process is not None
    assert invalid._process.poll() is not None


def test_turn_transport_and_target_failures_are_not_retried(tmp_path: Path) -> None:
    script = _fault_script(tmp_path / "fault.py")

    crashed = _session(script, "crash_turn")
    crashed.open(kensa_case(id="case", input="hello"))
    with pytest.raises(KensaEvalError) as transport:
        crashed.respond(({"role": "user", "content": "hello"},))
    assert transport.value.failure.kind == "target_exit"
    assert transport.value.failure.evidence["last_completed_operation"] == "open_session"
    assert crashed._process is not None
    assert crashed._process.poll() == 9

    declared = _session(script, "turn_error")
    declared.open(kensa_case(id="case", input="hello"))
    with pytest.raises(RuntimeError, match="target responder failed") as responder:
        declared.respond(({"role": "user", "content": "hello"},))
    assert "private diagnostic" in "\n".join(getattr(responder.value, "__notes__", []))
    process = declared._process
    declared.close()
    assert process is not None
    assert process.poll() == 0


@pytest.mark.parametrize("behavior", ["output_nan", "output_infinity", "output_overflow"])
def test_nonstandard_response_numbers_abort_without_retry_and_reap_process(
    tmp_path: Path,
    behavior: str,
) -> None:
    script = _fault_script(tmp_path / "fault.py")
    session = _session(script, behavior, timeout_s=0.1)
    session.open(kensa_case(id="case", input="hello"))

    with pytest.raises(KensaEvalError, match="malformed protocol output") as raised:
        session.respond(({"role": "user", "content": "hello"},))

    assert raised.value.failure.category == "infrastructure"
    assert raised.value.failure.kind == "target_protocol"
    assert raised.value.failure.evidence == {
        "operation": "turn",
        "last_completed_operation": "open_session",
    }
    assert session._request_sequence == 3
    assert session._process is not None
    assert session._process.poll() is not None


@pytest.mark.parametrize(
    ("behavior", "kind", "message"),
    [
        ("cleanup_error", "target_cleanup", "target_close_failed"),
        ("hang_exit", "target_timeout", "process exit"),
        ("nonzero_exit", "target_exit", "before completing shutdown"),
        ("extra_output", "target_protocol", "output after shutdown"),
    ],
)
def test_cleanup_failures_reap_process(
    tmp_path: Path,
    behavior: str,
    kind: str,
    message: str,
) -> None:
    script = _fault_script(tmp_path / "fault.py")
    session = _session(script, behavior, timeout_s=0.1)
    session.open(kensa_case(id="case", input="hello"))
    session.respond(({"role": "user", "content": "hello"},))
    process = session._process

    with pytest.raises(KensaEvalError, match=message) as raised:
        session.close()

    assert raised.value.failure.kind == kind
    assert process is not None
    assert process.poll() is not None
    if behavior == "cleanup_error":
        assert "cleanup private diagnostic" in "\n".join(getattr(raised.value, "__notes__", []))


def test_write_timeout_aborts_without_replaying_open(tmp_path: Path) -> None:
    script = _fault_script(tmp_path / "fault.py")
    session = _session(script, "write_timeout", timeout_s=0.05)
    case = kensa_case(id="large", input="x" * 2_000_000)

    with pytest.raises(KensaEvalError, match="timed out during write") as raised:
        session.open(case)

    assert raised.value.failure.kind == "target_timeout"
    assert raised.value.failure.evidence["operation"] == "open_session"
    assert raised.value.failure.evidence["last_completed_operation"] == "handshake"
    assert session._request_sequence == 2
    assert session._process is not None
    assert session._process.poll() is not None


def test_unopened_session_closes_and_requires_a_process(tmp_path: Path) -> None:
    session = TargetCommandSession(("unused",), timeout_s=0.1, cwd=tmp_path)

    with pytest.raises(RuntimeError, match="not running"):
        session._require_process()
    session.close()
    session.close()


def test_nonblocking_io_retries_interrupted_reads_and_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _fault_script(tmp_path / "fault.py")
    session = _session(script, "success")
    real_write = os.write
    real_read = os.read
    write_interrupted = False
    read_interrupted = False
    drain_interrupted = False

    def flaky_write(fd: int, payload: bytes) -> int:
        nonlocal write_interrupted
        if (
            session._process is not None
            and session._process.stdin is not None
            and fd == session._process.stdin.fileno()
            and not write_interrupted
        ):
            write_interrupted = True
            raise BlockingIOError
        return real_write(fd, payload)

    def flaky_read(fd: int, size: int) -> bytes:
        nonlocal read_interrupted, drain_interrupted
        if (
            session._process is not None
            and session._process.stdout is not None
            and fd == session._process.stdout.fileno()
            and not read_interrupted
        ):
            read_interrupted = True
            raise BlockingIOError
        if (
            session._process is not None
            and session._process.poll() is not None
            and not drain_interrupted
        ):
            drain_interrupted = True
            raise BlockingIOError
        return real_read(fd, size)

    monkeypatch.setattr("kensa.target_client.os.write", flaky_write)
    monkeypatch.setattr("kensa.target_client.os.read", flaky_read)

    session.open(kensa_case(id="case", input="hello"))
    assert session.respond(({"role": "user", "content": "hello"},)).content == "reply"
    session.close()

    assert write_interrupted is True
    assert read_interrupted is True
    assert drain_interrupted is True


@pytest.mark.parametrize("write_result", ["broken", "zero"])
def test_ambiguous_write_aborts_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_result: str,
) -> None:
    script = _fault_script(tmp_path / "fault.py")
    session = _session(script, "success")
    real_write = os.write
    writes = 0

    def interrupted_write(fd: int, payload: bytes) -> int:
        nonlocal writes
        if (
            session._process is None
            or session._process.stdin is None
            or fd != session._process.stdin.fileno()
        ):
            return real_write(fd, payload)
        writes += 1
        if writes == 2:
            if write_result == "broken":
                raise BrokenPipeError
            return 0
        return real_write(fd, payload)

    monkeypatch.setattr("kensa.target_client.os.write", interrupted_write)

    with pytest.raises(KensaEvalError) as raised:
        session.open(kensa_case(id="case", input="hello"))

    assert raised.value.failure.kind == "target_exit"
    assert raised.value.failure.evidence["operation"] == "open_session"
    assert writes == 2
    assert session._request_sequence == 2
    assert session._process is not None
    assert session._process.poll() is not None


def test_configured_fixture_matches_in_process_case_results(
    pytester: pytest.Pytester,
) -> None:
    root = Path(str(pytester.path))
    log = root / "target.jsonl"
    script = _host_script(root / "target.py")
    _configure_target(root, (sys.executable, str(script), str(log)))
    pytester.makepyfile(
        test_eval="""
        import pytest
        from kensa.pytest import ConversationResponse, kensa_case


        class Simulator:
            def respond(self, messages):
                return ConversationResponse(content="simulated user")


        @pytest.mark.kensa
        @pytest.mark.parametrize("case", [kensa_case(id="direct", input="hello")])
        def test_direct(case, kensa_run):
            case.run(kensa_run)


        @pytest.mark.kensa
        @pytest.mark.asyncio
        @pytest.mark.parametrize(
            "case",
            [kensa_case(id="simulated", input="hello", termination_reason="done")],
        )
        async def test_simulated(case, kensa_run):
            await case.run(
                kensa_run,
                simulator=Simulator(),
                max_turns=2,
                starts_with="simulator",
            )
        """
    )

    command_run = pytester.runpytest("-q", "--kensa-write-artifacts")

    command_run.assert_outcomes(passed=2)
    command_artifacts = set((root / ".kensa" / "results").glob("*.json"))
    assert len(command_artifacts) == 1
    command_trials = json.loads(next(iter(command_artifacts)).read_text())["trials"]

    pytester.makeconftest(
        """
        import pytest
        from kensa.pytest import ConversationResponse


        @pytest.fixture
        def kensa_run(case):
            class Agent:
                def respond(self, messages):
                    return ConversationResponse(
                        content=f"reply:{len(messages)}",
                        output={"case": case.id, "messages": len(messages)},
                        termination_reason=case.row.get("termination_reason"),
                    )
            return Agent()
        """
    )

    in_process_run = pytester.runpytest("-q", "--kensa-write-artifacts")

    in_process_run.assert_outcomes(passed=2)
    all_artifacts = set((root / ".kensa" / "results").glob("*.json"))
    in_process_artifacts = all_artifacts - command_artifacts
    assert len(in_process_artifacts) == 1
    in_process_trials = json.loads(next(iter(in_process_artifacts)).read_text())["trials"]
    assert len(command_trials) == len(in_process_trials) == 2
    command_results = {trial["case_id"]: trial["output"] for trial in command_trials}
    in_process_results = {trial["case_id"]: trial["output"] for trial in in_process_trials}
    assert command_results == in_process_results


def test_configured_fixture_persists_evidence_in_trial_snapshot(
    pytester: pytest.Pytester,
) -> None:
    root = Path(str(pytester.path))
    log = root / "target.jsonl"
    script = _host_script(root / "target.py")
    _configure_target(root, (sys.executable, str(script), str(log)))
    pytester.makepyfile(
        test_eval="""
        import json
        from pathlib import Path

        import pytest
        from kensa.pytest import kensa_case


        @pytest.mark.kensa
        @pytest.mark.parametrize(
            "case",
            [kensa_case(id="snapshot", input="hello", evidence=True)],
        )
        def test_snapshot(case, kensa_run, kensa_trace):
            case.run(kensa_run)
            assert len(kensa_trace.agent_runs) == 1
            serialized = kensa_trace.agent_runs[0].model_dump(mode="json")
            snapshot_path = next(Path(".kensa/results").glob("*.json"))
            snapshot = json.loads(snapshot_path.read_text())
            assert snapshot["complete"] is False
            assert snapshot["trials"][0]["status"] == "provisional"
            assert snapshot["trials"][0]["trace"]["agent_runs"] == [serialized]
        """
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(passed=1)
    artifact = next((root / ".kensa" / "results").glob("*.json"))
    trial = json.loads(artifact.read_text())["trials"][0]
    _assert_serialized_evidence(
        trial["trace"]["agent_runs"][0],
        case_id="snapshot",
        complete=True,
    )


@pytest.mark.parametrize("xdist", [False, True])
def test_configured_fixture_runs_fresh_processes_and_preserves_evidence(
    pytester: pytest.Pytester,
    xdist: bool,
) -> None:
    root = Path(str(pytester.path))
    log = root / "target.jsonl"
    script = _host_script(root / "target.py")
    _configure_target(root, (sys.executable, str(script), str(log)))
    pytester.makepyfile(
        test_eval="""
        import pytest
        from kensa.pytest import kensa_case


        def assert_evidence(run, case):
            assert run.schema_version == "kensa.agent_run.v1"
            assert run.run_id.endswith(case.id)
            assert run.attestation.revision == "revision-1"
            assert run.attestation.environment == "sandbox"
            assert run.attestation.effects == "sandboxed"
            assert len(run.events) == 1
            assert run.events[0].id == f"event-{run.run_id}"
            assert run.events[0].sequence == 1
            assert run.events[0].kind == "action"
            assert run.events[0].name == "configured-target"
            assert run.events[0].status == "completed"
            assert len(run.state) == 1
            assert run.state[0].name == "session"
            assert run.state[0].value == {"sentinel": run.run_id}
            assert run.state[0].source == "target"
            complete = case.row.get("complete", True)
            assert run.trajectory_completeness == ("complete" if complete else "partial")
            assert run.state_completeness == ("complete" if complete else "unavailable")
            assert run.incomplete_reason == (
                None if complete else "target omitted some evidence"
            )


        @pytest.mark.kensa(trials=2)
        @pytest.mark.parametrize(
            "case",
            [
                kensa_case(id="complete", input="hello", evidence=True),
                kensa_case(
                    id="partial",
                    input="hello",
                    evidence=True,
                    complete=False,
                ),
            ],
        )
        def test_configured(case, kensa_run, kensa_trace):
            result = case.run(kensa_run)
            assert result.output == {"case": case.id, "messages": 0}
            assert result.messages[-1] == {"role": "assistant", "content": "reply:0"}
            assert len(kensa_trace.agent_runs) == 1
            assert_evidence(kensa_trace.agent_runs[0], case)


        class Simulator:
            def respond(self, messages):
                from kensa.pytest import ConversationResponse
                return ConversationResponse(content="simulated user")


        @pytest.mark.kensa
        @pytest.mark.asyncio
        @pytest.mark.parametrize(
            "case",
            [kensa_case(id="simulated", input="hello", termination_reason="done")],
        )
        async def test_simulated(case, kensa_run):
            result = await case.run(
                kensa_run,
                simulator=Simulator(),
                max_turns=2,
                starts_with="simulator",
            )
            assert result.output == {"case": "simulated", "messages": 1}
            assert result.messages == (
                {"role": "user", "content": "simulated user"},
                {"role": "assistant", "content": "reply:1"},
            )
            assert result.termination.source == "agent"
            assert result.termination.reason == "done"
        """
    )

    args = ["-q", "--kensa-write-artifacts"]
    if xdist:
        args.extend(["-n", "2", "--dist=load"])
    result = pytester.runpytest(*args)

    result.assert_outcomes(passed=5)
    events = [json.loads(line) for line in log.read_text().splitlines()]
    opens = [event for event in events if event["event"] == "open"]
    closes = [event for event in events if event["event"] == "close"]
    assert len(opens) == len(closes) == 5
    assert len({event["pid"] for event in opens}) == 5
    assert len({event["sentinel"] for event in opens}) == 5
    assert {event["sentinel"] for event in opens} == {event["sentinel"] for event in closes}
    artifact = next((root / ".kensa" / "results").glob("*.json"))
    trials = json.loads(artifact.read_text())["trials"]
    assert len(trials) == 5
    evidence_trials = [trial for trial in trials if trial["case_id"] != "simulated"]
    assert len({trial["trace"]["agent_runs"][0]["run_id"] for trial in evidence_trials}) == 4
    for trial in evidence_trials:
        _assert_serialized_evidence(
            trial["trace"]["agent_runs"][0],
            case_id=trial["case_id"],
            complete=trial["case_id"] == "complete",
        )
    partial = next(trial for trial in trials if trial["case_id"] == "partial")
    run = partial["trace"]["agent_runs"][0]
    assert run["trajectory_completeness"] == "partial"
    assert run["state_completeness"] == "unavailable"
    assert run["incomplete_reason"] == "target omitted some evidence"
    trace_path = next((root / ".kensa" / "traces" / "runs").glob("*/trials.jsonl"))
    trace_rows = [json.loads(line) for line in trace_path.read_text().splitlines()]
    evidence_trace_rows = [row for row in trace_rows if row["case_id"] != "simulated"]
    assert len(evidence_trace_rows) == 4
    for row in evidence_trace_rows:
        _assert_serialized_evidence(
            row["agent_runs"][0],
            case_id=row["case_id"],
            complete=row["case_id"] == "complete",
        )
        artifact_trial = next(
            trial
            for trial in evidence_trials
            if trial["case_id"] == row["case_id"]
            and trial["trial_index"] == int(row["id"].rsplit("_trial", 1)[1])
        )
        assert row["agent_runs"] == artifact_trial["trace"]["agent_runs"]


@pytest.mark.parametrize(
    ("behavior", "category", "kind"),
    [
        ("turn_error", "agent", "execution"),
        ("crash_turn", "infrastructure", "target_exit"),
    ],
)
def test_configured_fixture_preserves_failure_ownership(
    pytester: pytest.Pytester,
    behavior: str,
    category: str,
    kind: str,
) -> None:
    root = Path(str(pytester.path))
    script = _fault_script(root / "fault.py")
    _configure_target(root, (sys.executable, str(script), behavior), timeout_s=0.2)
    pytester.makepyfile(
        test_eval="""
        import pytest
        from kensa.pytest import kensa_case


        @pytest.mark.kensa
        @pytest.mark.parametrize("case", [kensa_case(id="case", input="hello")])
        def test_failure(case, kensa_run):
            case.run(kensa_run)
        """
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(failed=1)
    artifact = next((root / ".kensa" / "results").glob("*.json"))
    failure = json.loads(artifact.read_text())["trials"][0]["failure"]
    assert failure["category"] == category
    assert failure["kind"] == kind
    assert "private diagnostic" not in json.dumps(failure)
    assert "crash diagnostic" not in json.dumps(failure)


def test_repository_fixture_overrides_configured_command(pytester: pytest.Pytester) -> None:
    root = Path(str(pytester.path))
    _configure_target(root, (str(root / "missing-target"),))
    pytester.makeconftest(
        """
        import pytest
        from kensa.pytest import ConversationResponse


        @pytest.fixture
        def kensa_run():
            class Agent:
                def respond(self, messages):
                    return ConversationResponse(output={"source": "repository"})
            return Agent()
        """
    )
    pytester.makepyfile(
        test_eval="""
        import pytest
        from kensa.pytest import kensa_case


        @pytest.mark.kensa
        @pytest.mark.parametrize("case", [kensa_case(id="case", input="hello")])
        def test_override(case, kensa_run):
            assert case.run(kensa_run).output == {"source": "repository"}
        """
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=1)


def test_configured_fixture_resolves_computed_case_fixture(pytester: pytest.Pytester) -> None:
    root = Path(str(pytester.path))
    log = root / "target.jsonl"
    script = _host_script(root / "target.py")
    _configure_target(root, (sys.executable, str(script), str(log)))
    pytester.makeconftest(
        """
        import pytest
        from kensa.pytest import kensa_case


        @pytest.fixture
        def case():
            return kensa_case(id="computed", input="hello")
        """
    )
    pytester.makepyfile(
        test_eval="""
        import pytest


        @pytest.mark.kensa
        def test_computed(case, kensa_run):
            assert case.run(kensa_run).output == {"case": "computed", "messages": 0}
        """
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=1)


def test_configured_fixture_requires_exactly_one_case(pytester: pytest.Pytester) -> None:
    root = Path(str(pytester.path))
    _configure_target(root, (str(root / "unused"),))
    pytester.makepyfile(
        test_eval="""
        import pytest


        @pytest.mark.kensa
        def test_missing_case(kensa_run):
            pass
        """
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*requires exactly one KensaCase fixture value*"])


def test_invalid_target_configuration_fails_pytest_startup(pytester: pytest.Pytester) -> None:
    root = Path(str(pytester.path))
    (root / "pyproject.toml").write_text("[tool.kensa]\ntarget_command = []\n")
    pytester.makepyfile("def test_never_runs():\n    pass\n")

    result = pytester.runpytest("-q")

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*invalid Kensa configuration in*pyproject.toml*"])


def test_unconfigured_project_keeps_fixture_not_found_behavior(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_eval="""
        def test_unconfigured(kensa_run):
            pass
        """
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*fixture 'kensa_run' not found*"])
