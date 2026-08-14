"""Subprocess client for configured target command sessions."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, BinaryIO, TypeVar, cast
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from kensa.case import KensaCase, KensaMessage
from kensa.conversation import ConversationResponse
from kensa.errors import FailureCategory, KensaEvalError
from kensa.target import attach_agent_run
from kensa.target_command import (
    _REQUEST_ADAPTER,
    _RESPONSE_ADAPTER,
    TARGET_PROTOCOL_VERSION,
    _ErrorResponse,
    _HandshakeResponse,
    _SessionClosedResponse,
    _SessionOpenedResponse,
    _ShutdownResponse,
    _TurnResponse,
)

_ResponseT = TypeVar("_ResponseT", bound=BaseModel)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _validate_standard_json(raw: str) -> None:
    payload = json.loads(raw, parse_constant=_reject_json_constant)
    json.dumps(payload, allow_nan=False)


class _TargetResponderFailure(RuntimeError):
    pass


class TargetCommandSession:
    """One subprocess-backed conversation agent and target session."""

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        timeout_s: float,
        cwd: Path,
    ) -> None:
        self._command = command
        self._timeout_s = timeout_s
        self._cwd = cwd
        self._process: subprocess.Popen[bytes] | None = None
        self._resources = ExitStack()
        self._stderr: BinaryIO | None = None
        self._stdout_buffer = bytearray()
        self._session_id = uuid4().hex
        self._request_sequence = 0
        self._last_completed_operation = "none"
        self._opened = False
        self._closed = False

    def open(self, case: KensaCase) -> None:
        if self._process is not None or self._closed:
            raise RuntimeError("target command session may be opened only once")
        try:
            self._spawn()
            self._exchange(
                "handshake",
                {"type": "handshake", "version": TARGET_PROTOCOL_VERSION},
                _HandshakeResponse,
            )
            self._exchange(
                "open_session",
                {
                    "type": "open_session",
                    "session_id": self._session_id,
                    "case": {"id": case.id, "row": dict(case.row)},
                },
                _SessionOpenedResponse,
            )
            self._opened = True
        except KensaEvalError as error:
            self._abort(error)
            raise

    def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
        if not self._opened or self._closed:
            raise RuntimeError("target command session is not active")
        try:
            response = self._exchange(
                "turn",
                {
                    "type": "turn",
                    "session_id": self._session_id,
                    "messages": list(messages),
                },
                _TurnResponse,
            )
        except _TargetResponderFailure as error:
            self._annotate(error)
            raise
        except KensaEvalError as error:
            self._abort(error)
            raise
        if response.evidence is not None:
            attach_agent_run(response.evidence)
        return response.response

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process is None:
            self._close_resources()
            return
        failure: KensaEvalError | None = None
        try:
            if self._opened:
                self._exchange(
                    "close_session",
                    {"type": "close_session", "session_id": self._session_id},
                    _SessionClosedResponse,
                )
                self._opened = False
            self._exchange("shutdown", {"type": "shutdown"}, _ShutdownResponse)
            self._close_stdin()
            self._wait_for_exit()
        except KensaEvalError as error:
            failure = error
        finally:
            self._terminate()
            self._annotate(failure)
            self._close_resources()
        if failure is not None:
            raise failure

    def _spawn(self) -> None:
        stderr_fd, stderr_path = tempfile.mkstemp(prefix="kensa-target-stderr-")
        os.unlink(stderr_path)
        self._stderr = self._resources.enter_context(os.fdopen(stderr_fd, "w+b"))
        try:
            process = subprocess.Popen(
                self._command,
                cwd=self._cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                shell=False,
            )
        except OSError as exc:
            raise self._failure(
                "target_startup",
                "target command could not start",
                operation="startup",
                details={"exception_type": type(exc).__name__},
            ) from exc
        self._process = process
        stdin = cast(BinaryIO, process.stdin)
        stdout = cast(BinaryIO, process.stdout)
        os.set_blocking(stdin.fileno(), False)
        os.set_blocking(stdout.fileno(), False)

    def _exchange(
        self,
        operation: str,
        payload: dict[str, Any],
        response_type: type[_ResponseT],
    ) -> _ResponseT:
        self._request_sequence += 1
        request_id = f"request-{self._request_sequence}"
        request_payload = {**payload, "request_id": request_id}
        try:
            request = _REQUEST_ADAPTER.validate_python(request_payload)
            serialized = request.model_dump(mode="json", exclude_unset=True)
            frame = json.dumps(serialized, separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError, ValidationError) as exc:
            raise self._failure(
                "target_request",
                f"target {operation} request is invalid",
                operation=operation,
                category="harness",
                details={"exception_type": type(exc).__name__},
            ) from exc
        self._write(frame + b"\n", operation)
        raw = self._read(operation)
        try:
            _validate_standard_json(raw)
            response = _RESPONSE_ADAPTER.validate_json(raw)
        except (TypeError, ValueError, ValidationError) as exc:
            raise self._protocol_failure(
                operation, "target returned malformed protocol output"
            ) from exc
        if response.request_id != request_id:
            raise self._protocol_failure(operation, "target response request_id did not match")
        if isinstance(response, _ErrorResponse):
            self._last_completed_operation = operation
            if operation == "turn" and response.code == "target_turn_failed":
                raise _TargetResponderFailure("target responder failed")
            kind = (
                "target_startup"
                if operation == "open_session"
                else "target_cleanup"
                if operation in {"close_session", "shutdown"}
                else "target_protocol"
            )
            raise self._failure(
                kind,
                f"target {operation} failed with {response.code}",
                operation=operation,
                details={"protocol_code": response.code},
            )
        if not isinstance(response, response_type):
            raise self._protocol_failure(operation, "target returned the wrong frame type")
        session_id = getattr(response, "session_id", self._session_id)
        if session_id != self._session_id:
            raise self._protocol_failure(operation, "target response session_id did not match")
        self._last_completed_operation = operation
        return response

    def _write(self, payload: bytes, operation: str) -> None:
        process = self._require_process()
        stdin = cast(BinaryIO, process.stdin)
        deadline = time.monotonic() + self._timeout_s
        offset = 0
        with selectors.DefaultSelector() as selector:
            selector.register(stdin, selectors.EVENT_WRITE)
            while offset < len(payload):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise self._timeout_failure(operation, "write")
                try:
                    written = os.write(stdin.fileno(), payload[offset:])
                except BlockingIOError:
                    continue
                except BrokenPipeError as exc:
                    raise self._exit_failure(operation) from exc
                if written == 0:
                    raise self._exit_failure(operation)
                offset += written

    def _read(self, operation: str) -> str:
        process = self._require_process()
        stdout = cast(BinaryIO, process.stdout)
        deadline = time.monotonic() + self._timeout_s
        while True:
            newline = self._stdout_buffer.find(b"\n")
            if newline >= 0:
                frame = bytes(self._stdout_buffer[:newline])
                del self._stdout_buffer[: newline + 1]
                try:
                    return frame.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise self._protocol_failure(
                        operation, "target returned non-UTF-8 protocol output"
                    ) from exc
            remaining = deadline - time.monotonic()
            with selectors.DefaultSelector() as selector:
                selector.register(stdout, selectors.EVENT_READ)
                if remaining <= 0 or not selector.select(remaining):
                    raise self._timeout_failure(operation, "response")
            try:
                chunk = os.read(stdout.fileno(), 65_536)
            except BlockingIOError:
                continue
            if chunk:
                self._stdout_buffer.extend(chunk)
                continue
            if self._stdout_buffer:
                raise self._protocol_failure(
                    operation, "target ended a protocol frame without a newline"
                )
            raise self._exit_failure(operation)

    def _wait_for_exit(self) -> None:
        process = self._require_process()
        try:
            returncode = process.wait(timeout=self._timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise self._timeout_failure("shutdown", "process exit") from exc
        if returncode != 0:
            raise self._exit_failure("shutdown")
        stdout = cast(BinaryIO, process.stdout)
        while True:
            try:
                chunk = os.read(stdout.fileno(), 65_536)
            except BlockingIOError:
                continue
            if not chunk:
                break
            self._stdout_buffer.extend(chunk)
        if self._stdout_buffer:
            raise self._protocol_failure("shutdown", "target wrote output after shutdown")

    def _require_process(self) -> subprocess.Popen[bytes]:
        if self._process is None:
            raise RuntimeError("target command process is not running")
        return self._process

    def _close_stdin(self) -> None:
        if self._process is not None and self._process.stdin is not None:
            self._process.stdin.close()

    def _terminate(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=self._timeout_s)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _abort(self, error: KensaEvalError) -> None:
        self._closed = True
        self._terminate()
        self._annotate(error)
        self._close_resources()

    def _close_resources(self) -> None:
        process = self._process
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.stdout is not None and not process.stdout.closed:
                process.stdout.close()
        if self._stderr is not None:
            self._resources.close()
            self._stderr = None

    def _annotate(self, error: BaseException | None) -> None:
        if error is None or self._stderr is None:
            return
        self._stderr.flush()
        size = os.fstat(self._stderr.fileno()).st_size
        stderr = os.pread(self._stderr.fileno(), size, 0).decode("utf-8", errors="replace").strip()
        if stderr:
            error.add_note(f"Target stderr:\n{stderr}")

    def _timeout_failure(self, operation: str, boundary: str) -> KensaEvalError:
        return self._failure(
            "target_timeout",
            f"target {operation} timed out during {boundary}",
            operation=operation,
            details={"boundary": boundary},
        )

    def _exit_failure(self, operation: str) -> KensaEvalError:
        process = self._require_process()
        return self._failure(
            "target_exit",
            f"target exited before completing {operation}",
            operation=operation,
            details={"returncode": process.poll()},
        )

    def _protocol_failure(self, operation: str, message: str) -> KensaEvalError:
        return self._failure("target_protocol", message, operation=operation)

    def _failure(
        self,
        kind: str,
        message: str,
        *,
        operation: str,
        category: FailureCategory = "infrastructure",
        details: dict[str, Any] | None = None,
    ) -> KensaEvalError:
        evidence = {
            "operation": operation,
            "last_completed_operation": self._last_completed_operation,
            **(details or {}),
        }
        return KensaEvalError(
            message,
            category=category,
            kind=kind,
            evidence=cast(Any, evidence),
        )


__all__ = ["TargetCommandSession"]
