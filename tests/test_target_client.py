from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from target_client_support import _fault_script, _host_script, _session

from kensa.case import KensaCase, kensa_case
from kensa.errors import KensaEvalError
from kensa.target_client import TargetCommandSession


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
    request_sequence = declared._request_sequence
    with pytest.raises(RuntimeError, match="cannot continue after a fatal error"):
        declared.respond(({"role": "user", "content": "retry"},))
    assert declared._request_sequence == request_sequence
    process = declared._process
    declared.close()
    assert process is not None
    assert process.poll() == 0


def test_target_stderr_diagnostic_is_bounded_to_its_tail(tmp_path: Path) -> None:
    script = _fault_script(tmp_path / "fault.py")
    session = _session(script, "turn_error_large_stderr")
    session.open(kensa_case(id="case", input="hello"))

    with pytest.raises(RuntimeError, match="target responder failed") as raised:
        session.respond(({"role": "user", "content": "hello"},))

    notes = "\n".join(getattr(raised.value, "__notes__", []))
    assert "truncated to last 65536 bytes" in notes
    assert "stderr-tail" in notes
    assert "stderr-head" not in notes
    assert len(notes.encode()) < 66_000
    session.close()


def test_close_uses_one_deadline_for_all_teardown_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = TargetCommandSession(("unused",), timeout_s=1.0, cwd=tmp_path)
    session._process = cast(Any, object())
    session._opened = True
    deadlines: list[float | None] = []

    def exchange(
        operation: str,
        payload: dict[str, Any],
        response_type: type[Any],
        *,
        deadline: float | None = None,
    ) -> Any:
        deadlines.append(deadline)
        return object()

    monkeypatch.setattr(session, "_exchange", exchange)
    monkeypatch.setattr(session, "_close_stdin", lambda: None)
    monkeypatch.setattr(session, "_wait_for_exit", deadlines.append)
    monkeypatch.setattr(session, "_terminate", deadlines.append)
    monkeypatch.setattr(session, "_close_resources", lambda: None)

    session.close()

    assert len(deadlines) == 4
    assert deadlines[0] is not None
    assert len(set(deadlines)) == 1


def test_wait_for_exit_fails_immediately_after_teardown_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        args = ("target",)

        def wait(self, timeout: float) -> int:
            raise AssertionError(f"unexpected wait with timeout {timeout}")

    session = TargetCommandSession(("unused",), timeout_s=1.0, cwd=tmp_path)
    session._process = cast(Any, Process())
    monkeypatch.setattr("kensa.target_client.time.monotonic", lambda: 2.0)

    with pytest.raises(KensaEvalError, match="process exit") as raised:
        session._wait_for_exit(1.0)

    assert raised.value.failure.kind == "target_timeout"


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
