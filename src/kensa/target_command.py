"""Versioned JSON Lines host for repository-owned agent sessions."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from collections.abc import Awaitable, Callable
from copy import deepcopy
from types import MappingProxyType
from typing import Annotated, Any, Literal, Protocol, TextIO, TypeAlias, TypeVar, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from kensa.case import KensaCase, KensaMessage, _validate_messages
from kensa.conversation import ConversationResponse
from kensa.target import AgentRunEvidence

TARGET_PROTOCOL_VERSION = "kensa.target.v1"

_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)
_T = TypeVar("_T")


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must contain non-whitespace text")
    return value


class _Request(BaseModel):
    model_config = _MODEL_CONFIG

    request_id: str

    _validate_request_id = field_validator("request_id")(_nonblank)


class _HandshakeRequest(_Request):
    type: Literal["handshake"]
    version: str

    _validate_version = field_validator("version")(_nonblank)


class _FunctionCall(BaseModel):
    model_config = _MODEL_CONFIG

    name: str
    arguments: str

    _validate_name = field_validator("name")(_nonblank)

    @field_validator("arguments")
    @classmethod
    def _validate_arguments(cls, value: str) -> str:
        try:
            parsed = json.loads(value, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("must contain a JSON object") from exc
        if not isinstance(parsed, dict):
            raise ValueError("must contain a JSON object")
        return value


class _ToolCall(BaseModel):
    model_config = _MODEL_CONFIG

    id: str
    type: Literal["function"]
    function: _FunctionCall

    _validate_id = field_validator("id")(_nonblank)


class _TextMessage(BaseModel):
    model_config = _MODEL_CONFIG

    role: Literal["system", "developer", "user"]
    content: str
    name: str | None = None


class _AssistantMessage(BaseModel):
    model_config = _MODEL_CONFIG

    role: Literal["assistant"]
    content: str | None = None
    name: str | None = None
    tool_calls: list[_ToolCall] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_response_shape(self) -> _AssistantMessage:
        if self.tool_calls is None and self.content is None:
            raise ValueError("assistant messages require content or tool_calls")
        return self


class _ToolMessage(BaseModel):
    model_config = _MODEL_CONFIG

    role: Literal["tool"]
    tool_call_id: str
    content: str

    _validate_tool_call_id = field_validator("tool_call_id")(_nonblank)


_WireMessage: TypeAlias = Annotated[
    _TextMessage | _AssistantMessage | _ToolMessage,
    Field(discriminator="role"),
]
_MESSAGES_ADAPTER = TypeAdapter(list[_WireMessage])


class _TargetCase(BaseModel):
    model_config = _MODEL_CONFIG

    id: str
    row: dict[str, JsonValue]

    _validate_id = field_validator("id")(_nonblank)

    @model_validator(mode="after")
    def _validate_initial_messages(self) -> _TargetCase:
        messages = self.row.get("messages")
        if isinstance(messages, list):
            _validated_messages(_MESSAGES_ADAPTER.validate_python(messages))
        return self

    def to_case(self) -> KensaCase:
        return KensaCase(id=self.id, row=MappingProxyType(deepcopy(self.row)))


class _OpenSessionRequest(_Request):
    type: Literal["open_session"]
    session_id: str
    case: _TargetCase

    _validate_session_id = field_validator("session_id")(_nonblank)


class _TurnRequest(_Request):
    type: Literal["turn"]
    session_id: str
    messages: list[_WireMessage]

    _validate_session_id = field_validator("session_id")(_nonblank)


class _CloseSessionRequest(_Request):
    type: Literal["close_session"]
    session_id: str

    _validate_session_id = field_validator("session_id")(_nonblank)


class _ShutdownRequest(_Request):
    type: Literal["shutdown"]


TargetRequest: TypeAlias = Annotated[
    _HandshakeRequest
    | _OpenSessionRequest
    | _TurnRequest
    | _CloseSessionRequest
    | _ShutdownRequest,
    Field(discriminator="type"),
]
_REQUEST_ADAPTER = TypeAdapter(TargetRequest)


class TargetSession(Protocol):
    """Repository-owned stateful conversation session."""

    def respond(
        self,
        messages: tuple[KensaMessage, ...],
    ) -> (
        ConversationResponse | TargetTurnResult | Awaitable[ConversationResponse | TargetTurnResult]
    ): ...


TargetSessionFactory: TypeAlias = Callable[
    [KensaCase],
    TargetSession | Awaitable[TargetSession],
]


class TargetTurnResult(BaseModel):
    """One conversation response with optional external run evidence."""

    model_config = _MODEL_CONFIG

    response: ConversationResponse
    evidence: AgentRunEvidence | None = None


class _Response(BaseModel):
    model_config = _MODEL_CONFIG

    request_id: str

    _validate_request_id = field_validator("request_id")(_nonblank)


class _HandshakeResponse(_Response):
    type: Literal["handshake"]
    version: Literal["kensa.target.v1"]


class _SessionOpenedResponse(_Response):
    type: Literal["session_opened"]
    session_id: str

    _validate_session_id = field_validator("session_id")(_nonblank)


class _TurnResponse(_Response):
    type: Literal["turn"]
    session_id: str
    response: ConversationResponse
    evidence: AgentRunEvidence | None = None

    _validate_session_id = field_validator("session_id")(_nonblank)


class _SessionClosedResponse(_Response):
    type: Literal["session_closed"]
    session_id: str

    _validate_session_id = field_validator("session_id")(_nonblank)


class _ShutdownResponse(_Response):
    type: Literal["shutdown"]


class _ErrorResponse(BaseModel):
    model_config = _MODEL_CONFIG

    type: Literal["error"]
    request_id: str | None
    code: str
    message: str
    fatal: bool

    _validate_request_id = field_validator("request_id")(
        lambda value: None if value is None else _nonblank(value)
    )
    _validate_code = field_validator("code")(_nonblank)
    _validate_message = field_validator("message")(_nonblank)


TargetResponse: TypeAlias = Annotated[
    _HandshakeResponse
    | _SessionOpenedResponse
    | _TurnResponse
    | _SessionClosedResponse
    | _ShutdownResponse
    | _ErrorResponse,
    Field(discriminator="type"),
]
_RESPONSE_ADAPTER = TypeAdapter(TargetResponse)


class _Host:
    def __init__(
        self,
        open_session: TargetSessionFactory,
        *,
        stdout: TextIO,
        stderr: TextIO,
    ) -> None:
        self._open_session = open_session
        self._stdout = stdout
        self._stderr = stderr
        self._handshaken = False
        self._opened = False
        self._failed = False
        self._session: TargetSession | None = None
        self._session_id: str | None = None
        self._accepted_messages: list[dict[str, JsonValue]] = []
        self._request_ids: set[str] = set()

    def process(self, line: str) -> bool:
        request_id = _extract_request_id(line)
        try:
            request = _parse_request(line)
        except _RequestError as exc:
            self._write_error(request_id, exc.code, exc.message, fatal=exc.fatal)
            return False
        if request.request_id in self._request_ids:
            self._write_error(
                request.request_id,
                "duplicate_request_id",
                "request_id must be unique within a target process",
            )
            return False
        self._request_ids.add(request.request_id)
        if isinstance(request, _HandshakeRequest):
            self._handshake(request)
        elif isinstance(request, _OpenSessionRequest):
            self._open(request)
        elif isinstance(request, _TurnRequest):
            self._turn(request)
        elif isinstance(request, _CloseSessionRequest):
            self._close(request)
        else:
            return self._shutdown(request)
        return False

    def finish_eof(self) -> int:
        if self._session is not None:
            self._cleanup("target process reached EOF with an active session")
        self._diagnostic("target process reached EOF before shutdown")
        return 1

    def _handshake(self, request: _HandshakeRequest) -> None:
        if self._handshaken:
            self._write_error(request.request_id, "invalid_state", "handshake already completed")
            return
        if request.version != TARGET_PROTOCOL_VERSION:
            self._write_error(
                request.request_id,
                "unsupported_version",
                f"target supports only {TARGET_PROTOCOL_VERSION}",
                fatal=True,
            )
            return
        self._handshaken = True
        self._write(
            {
                "type": "handshake",
                "request_id": request.request_id,
                "version": TARGET_PROTOCOL_VERSION,
            }
        )

    def _open(self, request: _OpenSessionRequest) -> None:
        if not self._require_handshake(request.request_id):
            return
        if self._opened:
            self._write_error(
                request.request_id,
                "invalid_state",
                "target process may open only one session",
            )
            return
        self._opened = True
        try:
            session = _resolve(self._open_session(request.case.to_case()))
            if not callable(getattr(session, "respond", None)):
                raise TypeError("opened session must provide respond(messages)")
        except Exception as exc:
            self._failed = True
            self._diagnostic(f"target session open failed: {exc}")
            self._write_error(
                request.request_id,
                "target_open_failed",
                "target session factory failed",
                fatal=True,
            )
            return
        self._session = session
        self._session_id = request.session_id
        initial = request.case.row.get("messages", [])
        self._accepted_messages = deepcopy(initial) if isinstance(initial, list) else []
        self._write(
            {
                "type": "session_opened",
                "request_id": request.request_id,
                "session_id": request.session_id,
            }
        )

    def _turn(self, request: _TurnRequest) -> None:
        if not self._require_active(request.request_id, request.session_id):
            return
        if self._failed:
            self._write_error(
                request.request_id,
                "invalid_state",
                "target session cannot continue after a failed turn",
                fatal=True,
            )
            return
        try:
            messages = _validated_messages(request.messages)
        except ValueError as exc:
            self._write_error(request.request_id, "invalid_messages", str(exc))
            return
        if messages[: len(self._accepted_messages)] != self._accepted_messages:
            self._write_error(
                request.request_id,
                "history_mismatch",
                "messages must include the complete accepted session history",
            )
            return
        try:
            session = cast(TargetSession, self._session)
            value = _resolve(session.respond(cast(tuple[KensaMessage, ...], tuple(messages))))
        except Exception as exc:
            self._failed = True
            self._diagnostic(f"target turn failed: {exc}")
            self._write_error(
                request.request_id,
                "target_turn_failed",
                "target responder failed",
                fatal=True,
            )
            return
        try:
            result = _turn_result(value)
            payload: dict[str, Any] = {
                "type": "turn",
                "request_id": request.request_id,
                "session_id": request.session_id,
                "response": result.response,
            }
            if result.evidence is not None:
                payload["evidence"] = result.evidence
            _validate_json(result.model_dump(mode="json"))
        except Exception as exc:
            self._failed = True
            self._diagnostic(f"target turn failed: {exc}")
            self._write_error(
                request.request_id,
                "invalid_response",
                "target response did not match the protocol",
                fatal=True,
            )
            return
        self._accepted_messages = deepcopy(messages)
        content = result.response.content
        if content is not None:
            self._accepted_messages.append({"role": "assistant", "content": content})
        self._write(payload)

    def _close(self, request: _CloseSessionRequest) -> None:
        if not self._require_active(request.request_id, request.session_id):
            return
        failure = self._cleanup("target session close failed")
        if failure is not None:
            self._write_error(
                request.request_id,
                "target_close_failed",
                "target session cleanup failed",
                fatal=True,
            )
            return
        self._write(
            {
                "type": "session_closed",
                "request_id": request.request_id,
                "session_id": request.session_id,
            }
        )

    def _shutdown(self, request: _ShutdownRequest) -> bool:
        if not self._require_handshake(request.request_id):
            return False
        if self._session is not None:
            self._write_error(
                request.request_id,
                "invalid_state",
                "close the active session before shutdown",
            )
            return False
        self._write({"type": "shutdown", "request_id": request.request_id})
        return True

    def _require_handshake(self, request_id: str) -> bool:
        if self._handshaken:
            return True
        self._write_error(request_id, "invalid_state", "handshake must complete first")
        return False

    def _require_active(self, request_id: str, session_id: str) -> bool:
        if not self._require_handshake(request_id):
            return False
        if self._session is None:
            self._write_error(request_id, "invalid_state", "no target session is active")
            return False
        if session_id != self._session_id:
            self._write_error(
                request_id,
                "session_mismatch",
                "session_id does not match the active target session",
            )
            return False
        return True

    def _cleanup(self, context: str) -> Exception | None:
        session = cast(TargetSession, self._session)
        self._session = None
        self._session_id = None
        close = getattr(session, "close", None)
        if close is None:
            return None
        if not callable(close):
            error = TypeError("session close attribute must be callable")
            self._diagnostic(f"{context}: {error}")
            return error
        try:
            _resolve(close())
        except Exception as exc:
            self._diagnostic(f"{context}: {exc}")
            return exc
        return None

    def _write_error(
        self,
        request_id: str | None,
        code: str,
        message: str,
        *,
        fatal: bool = False,
    ) -> None:
        self._write(
            {
                "type": "error",
                "request_id": request_id,
                "code": code,
                "message": message,
                "fatal": fatal,
            }
        )

    def _write(self, payload: dict[str, Any]) -> None:
        response = _RESPONSE_ADAPTER.validate_python(payload)
        exclude = (
            {"evidence"}
            if isinstance(response, _TurnResponse) and response.evidence is None
            else None
        )
        serialized = response.model_dump(mode="json", exclude=exclude)
        _validate_json(serialized)
        self._stdout.write(json.dumps(serialized, separators=(",", ":"), allow_nan=False) + "\n")
        self._stdout.flush()

    def _diagnostic(self, message: str) -> None:
        self._stderr.write(message + "\n")
        self._stderr.flush()


class _RequestError(ValueError):
    def __init__(self, code: str, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fatal = fatal


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _extract_request_id(line: str) -> str | None:
    try:
        payload = json.loads(line, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("request_id")
    return value if isinstance(value, str) and value.strip() else None


def _parse_request(line: str) -> TargetRequest:
    try:
        payload = json.loads(line, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _RequestError("invalid_json", "request must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise _RequestError("invalid_request", "request must be a JSON object")
    try:
        return _REQUEST_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise _RequestError("invalid_request", "request does not match the protocol") from exc


def _validated_messages(value: list[_WireMessage]) -> list[dict[str, JsonValue]]:
    messages = [
        cast(dict[str, JsonValue], message.model_dump(mode="json", exclude_unset=True))
        for message in value
    ]
    if not messages:
        return messages
    try:
        _validate_messages(cast(list[KensaMessage], messages))
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    return messages


def _turn_result(value: Any) -> TargetTurnResult:
    if isinstance(value, TargetTurnResult):
        return value
    if isinstance(value, ConversationResponse):
        return TargetTurnResult(response=value)
    raise TypeError("respond() must return ConversationResponse or TargetTurnResult")


async def _await_value(value: Awaitable[_T]) -> _T:
    return await value


def _resolve(value: _T | Awaitable[_T]) -> _T:
    if inspect.isawaitable(value):
        return asyncio.run(_await_value(cast(Awaitable[_T], value)))
    return value


def _validate_json(value: Any) -> None:
    json.dumps(value, allow_nan=False)


def serve_target(
    open_session: TargetSessionFactory,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Serve one repository-owned session over the target command protocol."""

    source = sys.stdin if stdin is None else stdin
    destination = sys.stdout if stdout is None else stdout
    diagnostics = sys.stderr if stderr is None else stderr
    host = _Host(open_session, stdout=destination, stderr=diagnostics)
    for line in source:
        if host.process(line):
            return 0
    return host.finish_eof()


def target_protocol_schema() -> dict[str, Any]:
    """Return the canonical request and response schemas for protocol implementers."""

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://kensa.sh/schemas/target-command-v1.json",
        "title": "Kensa target command protocol v1",
        "$defs": {
            "request": _REQUEST_ADAPTER.json_schema(),
            "response": _RESPONSE_ADAPTER.json_schema(),
        },
    }


__all__ = [
    "TARGET_PROTOCOL_VERSION",
    "TargetSession",
    "TargetSessionFactory",
    "TargetTurnResult",
    "serve_target",
    "target_protocol_schema",
]
