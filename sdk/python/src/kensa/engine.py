"""Persistent client for the Kensa semantic engine."""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Timer
from typing import Any, Literal, cast

from pydantic import ValidationError

from kensa.case import KensaMessage, _validate_messages
from kensa.errors import KensaCaseError

PROTOCOL_VERSION = "kensa.engine.v1"
_ENGINE_COMMAND = "KENSA_ENGINE_COMMAND"
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_RESPONSE_TIMEOUT_S = 5.0
_TRACE_INTEGER_KEYS = frozenset(
    {
        "end_time_unix_nano",
        "ended_at_unix_nano",
        "start_time_unix_nano",
        "started_at_unix_nano",
        "time_unix_nano",
        "timestamp",
        "timestamp_unix_nano",
    }
)


@dataclass(frozen=True)
class EngineCompletion:
    verdict: Literal["pass", "fail", "error", "skipped"]
    failure: dict[str, Any] | None
    checks: tuple[dict[str, Any], ...] = ()
    judges: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class EngineConversationAction:
    source: Literal["agent", "simulator"]
    messages: tuple[dict[str, Any], ...]
    response_index: int
    agent_responses: int
    accepted_messages: tuple[dict[str, Any], ...]
    accepted_output: Any
    accepted_output_recorded: bool


@dataclass(frozen=True)
class EngineConversationResult:
    messages: tuple[dict[str, Any], ...]
    output: Any
    output_recorded: bool
    termination_source: Literal["agent", "simulator", "engine"]
    termination_reason: str


EngineConversationStep = EngineConversationAction | EngineConversationResult


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
        self._closing = False
        self._handshake_complete = False
        self._active_evaluations: set[str] = set()
        try:
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
                raise KensaEngineError(
                    "Kensa engine returned an invalid handshake", code="handshake"
                )
            self._handshake_complete = True
        except BaseException:
            self.close()
            raise

    def start_case(self, evaluation_id: str, case: Mapping[str, Any]) -> None:
        with self._lock:
            response = self._request_locked(
                {"type": "start_case", "evaluation_id": evaluation_id, "case": dict(case)}
            )
            if response.get("type") != "action" or response.get("action") != "invoke_agent":
                raise KensaEngineError(
                    "Kensa engine did not request agent invocation",
                    code="protocol",
                )
            self._active_evaluations.add(evaluation_id)

    def complete_case(
        self,
        evaluation_id: str,
        *,
        observation: Mapping[str, Any],
        runtime_outcome: Mapping[str, Any],
        judges: Sequence[Mapping[str, Any]] = (),
    ) -> EngineCompletion:
        with self._lock:
            action = self._request_locked(
                {
                    "type": "observe",
                    "evaluation_id": evaluation_id,
                    "observation": dict(observation),
                }
            )
            if action.get("type") != "action" or action.get("action") != "evaluate_check":
                raise KensaEngineError(
                    "Kensa engine did not request check evaluation",
                    code="protocol",
                )
            response = self._request_locked(
                {
                    "type": "check",
                    "evaluation_id": evaluation_id,
                    "runtime_outcome": cast(
                        dict[str, Any],
                        _wire_json_value(dict(runtime_outcome)),
                    ),
                    "judges": [dict(judge) for judge in judges],
                }
            )
            evaluation = response.get("evaluation")
            if response.get("type") != "result" or not isinstance(evaluation, dict):
                raise KensaEngineError("Kensa engine returned an invalid result", code="protocol")
            verdict = evaluation.get("verdict")
            if verdict not in {"pass", "fail", "error", "skipped"}:
                raise KensaEngineError("Kensa engine returned an invalid verdict", code="protocol")
            if evaluation.get("phase") != "complete":
                raise KensaEngineError(
                    "Kensa engine returned a non-terminal result",
                    code="protocol",
                )
            terminal_failure = evaluation.get("failure")
            if terminal_failure is not None and not isinstance(terminal_failure, dict):
                raise KensaEngineError("Kensa engine returned an invalid failure", code="protocol")
            requires_failure = verdict in {"fail", "error", "skipped"}
            if requires_failure != (terminal_failure is not None):
                raise KensaEngineError(
                    "Kensa engine verdict contradicts failure provenance",
                    code="protocol",
                )
            checks = _record_collection(evaluation.get("checks"), boundary="checks")
            judge_results = _record_collection(evaluation.get("judges"), boundary="judges")
            self._active_evaluations.discard(evaluation_id)
            return EngineCompletion(
                verdict=cast(Literal["pass", "fail", "error", "skipped"], verdict),
                failure=terminal_failure,
                checks=checks,
                judges=judge_results,
            )

    def classify_runtime_outcome(
        self,
        outcome: Mapping[str, Any],
        *,
        current: Mapping[str, Any] | None = None,
    ) -> EngineCompletion:
        response = self._request(
            {
                "type": "classify_runtime_outcome",
                "current": cast(
                    dict[str, Any] | None,
                    _wire_json_value(current),
                ),
                "outcome": cast(
                    dict[str, Any],
                    _wire_json_value(dict(outcome)),
                ),
            }
        )
        if response.get("type") != "runtime_outcome":
            raise KensaEngineError(
                "Kensa engine returned an invalid runtime outcome",
                code="protocol",
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise KensaEngineError(
                "Kensa engine returned an invalid runtime outcome",
                code="protocol",
            )
        return _runtime_completion(result)

    def start_conversation(
        self,
        conversation_id: str,
        conversation: Mapping[str, Any],
    ) -> EngineConversationAction:
        response = self._request(
            {
                "type": "start_conversation",
                "conversation_id": conversation_id,
                "conversation": dict(conversation),
            }
        )
        step = _conversation_step(response, conversation_id)
        if not isinstance(step, EngineConversationAction):
            raise KensaEngineError(
                "Kensa engine completed a conversation before a response",
                code="protocol",
            )
        return step

    def observe_conversation(
        self,
        conversation_id: str,
        observation: Mapping[str, Any],
    ) -> EngineConversationStep:
        response = self._request(
            {
                "type": "observe_conversation",
                "conversation_id": conversation_id,
                "observation": dict(observation),
            }
        )
        return _conversation_step(response, conversation_id)

    def cancel_case(self, evaluation_id: str, reason: str) -> None:
        with self._lock:
            self._cancel_locked(evaluation_id, reason)

    def cancel_all(self, reason: str) -> None:
        with self._lock:
            first_error: KensaEngineError | None = None
            for evaluation_id in tuple(self._active_evaluations):
                try:
                    self._cancel_locked(evaluation_id, reason)
                except KensaEngineError as exc:
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error

    def build_run(
        self,
        *,
        run_id: str,
        complete: bool,
        interruption: Mapping[str, Any] | None,
        trials: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        wire_trials = [
            cast(
                dict[str, Any],
                _wire_json_value(
                    dict(trial),
                    exact_integer_keys=_TRACE_INTEGER_KEYS,
                ),
            )
            for trial in trials
        ]
        expected_trials = sorted(
            wire_trials,
            key=lambda trial: (
                trial.get("group_id"),
                trial.get("trial_index"),
                trial.get("nodeid"),
            ),
        )
        wire_interruption = cast(
            dict[str, Any] | None,
            _wire_json_value(interruption),
        )
        expected_interruption = (
            {
                **wire_interruption,
                "nodeid": wire_interruption.get("nodeid"),
                "case_id": wire_interruption.get("case_id"),
                "trial_index": wire_interruption.get("trial_index"),
                "phase": wire_interruption.get("phase"),
            }
            if wire_interruption is not None
            else None
        )
        response = self._request(
            {
                "type": "build_run",
                "run_id": run_id,
                "complete": complete,
                "interruption": wire_interruption,
                "trials": wire_trials,
            }
        )
        result = response.get("result")
        if response.get("type") != "run_result" or not isinstance(result, dict):
            raise KensaEngineError("Kensa engine returned an invalid run result", code="protocol")
        from kensa.results import RunResult

        try:
            validated_result = RunResult.model_validate_json(
                json.dumps(result, allow_nan=False)
            ).model_dump(mode="json")
        except (TypeError, ValueError, ValidationError) as exc:
            raise KensaEngineError(
                "Kensa engine returned an invalid run result",
                code="protocol",
            ) from exc
        if (
            validated_result["run_id"] != run_id
            or validated_result["complete"] is not complete
            or not _json_values_equal(
                validated_result["interruption"],
                expected_interruption,
            )
            or not _json_values_equal(validated_result["trials"], expected_trials)
        ):
            raise KensaEngineError(
                "Kensa engine returned a contradictory run result",
                code="protocol",
            )
        return validated_result

    def reset(self) -> int:
        with self._lock:
            response = self._request_locked({"type": "reset"})
            released = response.get("released")
            if (
                response.get("type") != "reset"
                or not isinstance(released, int)
                or isinstance(released, bool)
                or released < 0
            ):
                raise KensaEngineError("Kensa engine returned an invalid reset", code="protocol")
            self._active_evaluations.clear()
            return released

    def normalize_trace_views(
        self,
        traces: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        wire_traces = cast(
            list[Any],
            _wire_json_value(
                [dict(trace) for trace in traces],
                exact_integer_keys=_TRACE_INTEGER_KEYS,
            ),
        )
        response = self._request({"type": "normalize_traces", "traces": wire_traces})
        normalized = response.get("traces")
        if (
            response.get("type") != "trace_views"
            or not isinstance(normalized, list)
            or not all(isinstance(trace, dict) for trace in normalized)
        ):
            raise KensaEngineError(
                "Kensa engine returned invalid trace views",
                code="protocol",
            )
        normalized_traces = cast(list[dict[str, Any]], normalized)
        requested_ids = [trace.get("id") for trace in traces]
        normalized_by_id = {
            trace.get("id"): trace
            for trace in normalized_traces
            if isinstance(trace.get("id"), str)
        }
        if (
            any(not isinstance(trace_id, str) for trace_id in requested_ids)
            or len(normalized_by_id) != len(normalized_traces)
            or set(requested_ids) != set(normalized_by_id)
        ):
            raise KensaEngineError(
                "Kensa engine returned trace views with contradictory identities",
                code="protocol",
            )
        return [normalized_by_id[trace_id] for trace_id in requested_ids]

    def close(self, *, notify_engine: bool = True) -> KensaEngineError | None:
        pending: BaseException | None = None
        shutdown_error: KensaEngineError | None = None
        with self._lock:
            if self._closed or self._closing:
                return None
            self._closing = True
            try:
                if self._handshake_complete and notify_engine:
                    for evaluation_id in tuple(self._active_evaluations):
                        try:
                            self._cancel_locked(evaluation_id, "Python engine client closed")
                        except KensaEngineError as exc:
                            if shutdown_error is None:
                                shutdown_error = exc
                            continue
                    try:
                        self._request_locked({"type": "reset"}, allow_closing=True)
                    except KensaEngineError as exc:
                        if shutdown_error is None:
                            shutdown_error = exc
            except BaseException as exc:
                pending = exc
            finally:
                self._active_evaluations.clear()
                self._close_stdin()
                self._reap_process()
                self._closed = True
                self._closing = False
        if pending is not None and not isinstance(pending, Exception):
            raise pending
        if pending is not None and shutdown_error is None:
            shutdown_error = KensaEngineError(
                f"Kensa engine shutdown failed: {pending}",
                code="shutdown",
            )
        return shutdown_error

    def _request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._request_locked(request)

    def _request_locked(
        self,
        request: Mapping[str, Any],
        *,
        allow_closing: bool = False,
    ) -> dict[str, Any]:
        if self._closed or (self._closing and not allow_closing):
            raise KensaEngineError("Kensa engine client is closed", code="closed")
        self._request_number += 1
        request_id = str(self._request_number)
        stdin = self._process.stdin
        stdout = self._process.stdout
        if stdin is None or stdout is None:
            raise KensaEngineError("Kensa engine pipes are unavailable", code="transport")
        try:
            wire = _wire_json_value({"id": request_id, "request": request})
            payload = json.dumps(wire, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise KensaEngineError(
                f"Kensa engine request violates the JSON contract: {exc}",
                code="invalid_message",
            ) from exc
        try:
            stdin.write(payload + "\n")
            stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise KensaEngineError(
                f"Could not write to Kensa engine: {exc}", code="transport"
            ) from exc
        timed_out = Event()

        def kill_timed_out_process() -> None:
            timed_out.set()
            with suppress(OSError):
                self._process.kill()

        timer = Timer(_RESPONSE_TIMEOUT_S, kill_timed_out_process)
        timer.daemon = True
        timer.start()
        try:
            line = stdout.readline()
        except (OSError, ValueError) as exc:
            raise KensaEngineError(
                f"Could not read from Kensa engine: {exc}",
                code="transport",
            ) from exc
        finally:
            timer.cancel()
        if not line:
            status = self._process.poll()
            code = "timeout" if timed_out.is_set() else "crash"
            message = (
                "Kensa engine timed out waiting for a response"
                if timed_out.is_set()
                else f"Kensa engine stopped before responding (exit status {status})"
            )
            raise KensaEngineError(message, code=code)
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError as exc:
            raise KensaEngineError("Kensa engine returned malformed JSON", code="protocol") from exc
        if not isinstance(envelope, dict) or envelope.get("id") != request_id:
            raise KensaEngineError("Kensa engine returned a mismatched response", code="protocol")
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

    def _cancel_locked(self, evaluation_id: str, reason: str) -> None:
        response = self._request_locked(
            {"type": "cancel", "evaluation_id": evaluation_id, "reason": reason},
            allow_closing=True,
        )
        evaluation = response.get("evaluation")
        if (
            response.get("type") != "result"
            or not isinstance(evaluation, dict)
            or evaluation.get("phase") != "cancelled"
        ):
            raise KensaEngineError("Kensa engine returned an invalid cancellation", code="protocol")
        self._active_evaluations.discard(evaluation_id)

    def _close_stdin(self) -> None:
        if self._process.stdin is None:
            return
        with suppress(OSError):
            self._process.stdin.close()

    def _reap_process(self) -> None:
        try:
            self._process.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        with suppress(OSError):
            self._process.terminate()
        try:
            self._process.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        with suppress(OSError):
            self._process.kill()
        with suppress(OSError):
            self._process.wait()

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
    repository = Path(__file__).resolve().parents[4]
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


def _record_collection(value: Any, *, boundary: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise KensaEngineError(
            f"Kensa engine returned invalid {boundary}",
            code="protocol",
        )
    return tuple(dict(item) for item in value)


def _runtime_completion(result: Mapping[str, Any]) -> EngineCompletion:
    verdict = result.get("verdict")
    failure = result.get("failure")
    check = result.get("check")
    if verdict not in {"pass", "fail", "error", "skipped"} or not isinstance(check, dict):
        raise KensaEngineError(
            "Kensa engine returned an invalid runtime outcome",
            code="protocol",
        )
    if failure is not None and not isinstance(failure, dict):
        raise KensaEngineError(
            "Kensa engine returned an invalid runtime outcome",
            code="protocol",
        )
    expected_outcome = {
        "pass": "satisfied",
        "fail": "unsatisfied",
        "error": "error",
        "skipped": "skipped",
    }[verdict]
    if (
        check.get("id") != "pytest"
        or check.get("outcome") != expected_outcome
        or not _json_values_equal(check.get("failure"), failure)
    ):
        raise KensaEngineError(
            "Kensa engine returned a contradictory runtime outcome",
            code="protocol",
        )
    return EngineCompletion(
        verdict=cast(Literal["pass", "fail", "error", "skipped"], verdict),
        failure=failure,
        checks=(dict(check),),
    )


def _conversation_step(
    response: Mapping[str, Any],
    conversation_id: str,
) -> EngineConversationStep:
    if response.get("conversation_id") != conversation_id:
        raise KensaEngineError(
            "Kensa engine returned a mismatched conversation",
            code="protocol",
        )
    response_type = response.get("type")
    if response_type == "conversation_action":
        return _conversation_action(response.get("action"))
    if response_type == "conversation_result":
        return _conversation_result(response.get("result"))
    raise KensaEngineError(
        "Kensa engine returned an invalid conversation response",
        code="protocol",
    )


def _conversation_action(value: Any) -> EngineConversationAction:
    if not isinstance(value, dict):
        raise KensaEngineError(
            "Kensa engine returned an invalid conversation action",
            code="protocol",
        )
    source = value.get("source")
    response_index = value.get("response_index")
    agent_responses = value.get("agent_responses")
    accepted = value.get("accepted")
    if (
        source not in {"agent", "simulator"}
        or not _is_integer(response_index, minimum=1)
        or not _is_integer(agent_responses, minimum=0)
        or not isinstance(accepted, dict)
    ):
        raise KensaEngineError(
            "Kensa engine returned an invalid conversation action",
            code="protocol",
        )
    output_recorded = accepted.get("output_recorded")
    if not isinstance(output_recorded, bool):
        raise KensaEngineError(
            "Kensa engine returned an invalid accepted conversation state",
            code="protocol",
        )
    output = _conversation_json(
        accepted.get("output"),
        boundary="accepted conversation output",
    )
    if not output_recorded and output is not None:
        raise KensaEngineError(
            "Kensa engine returned contradictory accepted conversation output",
            code="protocol",
        )
    return EngineConversationAction(
        source=cast(Literal["agent", "simulator"], source),
        messages=_conversation_messages(value.get("messages"), boundary="action messages"),
        response_index=cast(int, response_index),
        agent_responses=cast(int, agent_responses),
        accepted_messages=_conversation_messages(
            accepted.get("messages"),
            boundary="accepted messages",
        ),
        accepted_output=output,
        accepted_output_recorded=output_recorded,
    )


def _conversation_result(value: Any) -> EngineConversationResult:
    if not isinstance(value, dict) or value.get("phase") != "complete":
        raise KensaEngineError(
            "Kensa engine returned an invalid conversation result",
            code="protocol",
        )
    output_recorded = value.get("output_recorded")
    termination = value.get("termination")
    if not isinstance(output_recorded, bool) or not isinstance(termination, dict):
        raise KensaEngineError(
            "Kensa engine returned an invalid conversation result",
            code="protocol",
        )
    source = termination.get("source")
    reason = termination.get("reason")
    if source not in {"agent", "simulator", "engine"} or not _is_nonblank(reason):
        raise KensaEngineError(
            "Kensa engine returned an invalid conversation termination",
            code="protocol",
        )
    output = _conversation_json(value.get("output"), boundary="conversation output")
    if not output_recorded and output is not None:
        raise KensaEngineError(
            "Kensa engine returned contradictory conversation output",
            code="protocol",
        )
    return EngineConversationResult(
        messages=_conversation_messages(value.get("messages"), boundary="result messages"),
        output=output,
        output_recorded=output_recorded,
        termination_source=cast(Literal["agent", "simulator", "engine"], source),
        termination_reason=cast(str, reason),
    )


def _conversation_messages(value: Any, *, boundary: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise KensaEngineError(
            f"Kensa engine returned invalid {boundary}",
            code="protocol",
        )
    copied = _conversation_json(value, boundary=boundary)
    if copied:
        try:
            _validate_messages(cast(list[KensaMessage], copied))
        except KensaCaseError as exc:
            raise KensaEngineError(
                f"Kensa engine returned invalid {boundary}: {exc}",
                code="protocol",
            ) from exc
    return tuple(cast(list[dict[str, Any]], copied))


def _conversation_json(value: Any, *, boundary: str) -> Any:
    try:
        wire = _wire_json_value(value)
    except (TypeError, ValueError) as exc:
        raise KensaEngineError(
            f"Kensa engine returned invalid {boundary}",
            code="protocol",
        ) from exc
    if not _json_values_equal(value, wire):
        raise KensaEngineError(
            f"Kensa engine returned contradictory {boundary}",
            code="protocol",
        )
    return deepcopy(wire)


def _is_integer(value: Any, *, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _is_nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _json_values_equal(left: object, right: object) -> bool:
    if (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    ):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        left_items = cast(dict[object, object], left)
        right_items = cast(dict[object, object], right)
        return left_items.keys() == right_items.keys() and all(
            _json_values_equal(left_items[key], right_items[key]) for key in left_items
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _wire_json_value(
    value: Any,
    *,
    exact_integer_keys: frozenset[str] = frozenset(),
    field: str | None = None,
) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field or 'value'} contains a non-finite number")
        return value
    if isinstance(value, int):
        if field in exact_integer_keys:
            return str(value)
        if abs(value) <= _MAX_SAFE_INTEGER:
            return value
        location = field if field is not None else "value"
        raise ValueError(f"{location} contains an integer outside the interoperable JSON range")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if normalized_key in result:
                raise ValueError(
                    f"{field or 'value'} contains duplicate keys after string coercion"
                )
            result[normalized_key] = _wire_json_value(
                item,
                exact_integer_keys=exact_integer_keys,
                field=normalized_key,
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [
            _wire_json_value(
                item,
                exact_integer_keys=exact_integer_keys,
                field=field,
            )
            for item in value
        ]
    raise TypeError(f"{field or 'value'} is not JSON-serializable")
