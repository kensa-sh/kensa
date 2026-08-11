"""Persistent client for the Kensa semantic engine."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from threading import Lock
from typing import Any

PROTOCOL_VERSION = "kensa.engine.v1"
_ENGINE_COMMAND = "KENSA_ENGINE_COMMAND"
_MAX_SAFE_INTEGER = 9_007_199_254_740_991


class KensaEngineError(RuntimeError):
    """Engine startup, transport, or protocol failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "engine",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details) if details is not None else {}


class EngineClient:
    """One locked request stream to a persistent engine process."""

    def __init__(self, command: Sequence[str] | None = None) -> None:
        resolved = tuple(command) if command is not None else _engine_command()
        if not resolved:
            raise KensaEngineError("Kensa engine command must not be empty", code="startup")
        try:
            self._process = subprocess.Popen(
                resolved,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise KensaEngineError(f"Could not start Kensa engine: {exc}", code="startup") from exc
        self._lock = Lock()
        self._request_number = 0
        self._closed = False
        response = self._request(
            {
                "type": "handshake",
                "protocol_version": PROTOCOL_VERSION,
                "client": "kensa-python",
            }
        )
        if (
            response.get("type") != "handshake"
            or response.get("protocol_version") != PROTOCOL_VERSION
        ):
            self.close()
            raise KensaEngineError("Kensa engine returned an invalid handshake", code="handshake")

    def start_case(self, evaluation_id: str, case: Mapping[str, Any]) -> None:
        response = self._request(
            {"type": "start_case", "evaluation_id": evaluation_id, "case": dict(case)}
        )
        if response.get("type") != "action" or response.get("action") != "invoke_agent":
            raise KensaEngineError("Kensa engine did not request agent invocation", code="protocol")

    def complete_case(
        self,
        evaluation_id: str,
        *,
        observation: Mapping[str, Any],
        status: str,
        failure: Mapping[str, Any] | None,
    ) -> str:
        action = self._request(
            {
                "type": "observe",
                "evaluation_id": evaluation_id,
                "observation": dict(observation),
            }
        )
        if action.get("type") != "action" or action.get("action") != "evaluate_check":
            raise KensaEngineError("Kensa engine did not request check evaluation", code="protocol")
        response = self._request(
            {
                "type": "check",
                "evaluation_id": evaluation_id,
                "check": {
                    "id": "pytest",
                    "outcome": _check_outcome(status),
                    "failure": failure,
                },
            }
        )
        evaluation = response.get("evaluation")
        if response.get("type") != "result" or not isinstance(evaluation, dict):
            raise KensaEngineError("Kensa engine returned an invalid result", code="protocol")
        verdict = evaluation.get("verdict")
        if verdict not in {"pass", "fail", "error", "skipped"}:
            raise KensaEngineError("Kensa engine returned an invalid verdict", code="protocol")
        if evaluation.get("phase") != "complete" or verdict != status:
            raise KensaEngineError(
                "Kensa engine verdict contradicts the check observation",
                code="protocol",
            )
        requires_failure = verdict in {"fail", "error", "skipped"}
        if requires_failure != (failure is not None):
            raise KensaEngineError(
                "Kensa engine verdict contradicts failure provenance",
                code="protocol",
            )
        return verdict

    def cancel_case(self, evaluation_id: str, reason: str) -> None:
        response = self._request(
            {"type": "cancel", "evaluation_id": evaluation_id, "reason": reason}
        )
        evaluation = response.get("evaluation")
        if (
            response.get("type") != "result"
            or not isinstance(evaluation, dict)
            or evaluation.get("phase") != "cancelled"
        ):
            raise KensaEngineError("Kensa engine returned an invalid cancellation", code="protocol")

    def build_run(
        self,
        *,
        run_id: str,
        complete: bool,
        interruption: Mapping[str, Any] | None,
        trials: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        response = self._request(
            {
                "type": "build_run",
                "run_id": run_id,
                "complete": complete,
                "interruption": interruption,
                "trials": [dict(trial) for trial in trials],
            }
        )
        result = response.get("result")
        if response.get("type") != "run_result" or not isinstance(result, dict):
            raise KensaEngineError("Kensa engine returned an invalid run result", code="protocol")
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.stdin is not None:
            with suppress(OSError):
                self._process.stdin.close()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()

    def _request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise KensaEngineError("Kensa engine client is closed", code="closed")
            self._request_number += 1
            request_id = str(self._request_number)
            stdin = self._process.stdin
            stdout = self._process.stdout
            if stdin is None or stdout is None:
                raise KensaEngineError("Kensa engine pipes are unavailable", code="transport")
            try:
                payload = json.dumps(
                    _wire_json_value({"id": request_id, "request": request}),
                    allow_nan=False,
                )
                stdin.write(payload + "\n")
                stdin.flush()
            except (BrokenPipeError, OSError, TypeError, ValueError) as exc:
                raise KensaEngineError(
                    f"Could not write to Kensa engine: {exc}", code="transport"
                ) from exc
            line = stdout.readline()
            if not line:
                status = self._process.poll()
                raise KensaEngineError(
                    f"Kensa engine stopped before responding (exit status {status})",
                    code="crash",
                )
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError as exc:
                raise KensaEngineError(
                    "Kensa engine returned malformed JSON", code="protocol"
                ) from exc
            if not isinstance(envelope, dict) or envelope.get("id") != request_id:
                raise KensaEngineError(
                    "Kensa engine returned a mismatched response", code="protocol"
                )
            if envelope.get("ok") is not True:
                failure = envelope.get("failure")
                code = failure.get("code") if isinstance(failure, dict) else "protocol"
                message = failure.get("message") if isinstance(failure, dict) else None
                details = failure.get("details") if isinstance(failure, dict) else None
                raise KensaEngineError(
                    message if isinstance(message, str) else "Kensa engine request failed",
                    code=code if isinstance(code, str) else "protocol",
                    details=details if isinstance(details, dict) else None,
                )
            response = envelope.get("response")
            if not isinstance(response, dict):
                raise KensaEngineError("Kensa engine response is not an object", code="protocol")
            return response

    def __enter__(self) -> EngineClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _engine_command() -> tuple[str, ...]:
    configured = os.environ.get(_ENGINE_COMMAND)
    if configured is not None:
        return tuple(shlex.split(configured))
    executable = _engine_executable(os.name)
    bundled = Path(__file__).with_name("bin") / executable
    if bundled.is_file():
        return (str(bundled),)
    repository = Path(__file__).resolve().parents[2]
    development = repository / "packages" / "engine" / "dist" / "cli.js"
    node = shutil.which("node")
    if development.is_file() and node is not None:
        return (node, str(development))
    raise KensaEngineError(
        "Kensa engine executable is unavailable. Reinstall kensa or run pnpm build "
        "for development.",
        code="startup",
    )


def _engine_executable(platform_name: str) -> str:
    return "kensa-engine.exe" if platform_name == "nt" else "kensa-engine"


def _check_outcome(status: str) -> str:
    outcomes = {
        "pass": "satisfied",
        "fail": "unsatisfied",
        "error": "error",
        "skipped": "skipped",
    }
    try:
        return outcomes[status]
    except KeyError as exc:
        raise KensaEngineError(f"Unknown check status: {status!r}", code="protocol") from exc


def _wire_json_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (float, str)):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > _MAX_SAFE_INTEGER else value
    if isinstance(value, Mapping):
        return {str(key): _wire_json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_wire_json_value(item) for item in value]
    return value
