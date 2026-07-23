"""Case data and execution contract."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeAlias, cast, overload

import typing_extensions

from kensa.errors import KensaCaseError
from kensa.runtime import current_runtime

if TYPE_CHECKING:
    from kensa.conversation import (
        CaseResult,
        ConversationAgent,
        ConversationResponse,
        Simulator,
    )

_MISSING = object()


class _SyncConversationAgent(Protocol):
    def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse: ...


class _AsyncConversationAgent(Protocol):
    def respond(self, messages: tuple[KensaMessage, ...]) -> Awaitable[ConversationResponse]: ...


class KensaFunctionCall(typing_extensions.TypedDict):
    """OpenAI-compatible function call payload for assistant tool calls."""

    name: str
    arguments: str


class KensaToolCall(typing_extensions.TypedDict):
    """OpenAI-compatible function tool call used by any-llm completion."""

    id: str
    type: Literal["function"]
    function: KensaFunctionCall


class KensaSystemMessage(typing_extensions.TypedDict, total=False):
    """System message in Kensa's portable chat-completion subset."""

    role: typing_extensions.Required[Literal["system"]]
    content: typing_extensions.Required[str]
    name: typing_extensions.NotRequired[str]


class KensaDeveloperMessage(typing_extensions.TypedDict, total=False):
    """Developer message in Kensa's OpenAI-compatible chat-completion subset."""

    role: typing_extensions.Required[Literal["developer"]]
    content: typing_extensions.Required[str]
    name: typing_extensions.NotRequired[str]


class KensaUserMessage(typing_extensions.TypedDict, total=False):
    """User message in Kensa's portable chat-completion subset."""

    role: typing_extensions.Required[Literal["user"]]
    content: typing_extensions.Required[str]
    name: typing_extensions.NotRequired[str]


class _KensaAssistantTextMessage(typing_extensions.TypedDict, total=False):
    """Assistant text message in Kensa's portable chat-completion subset."""

    role: typing_extensions.Required[Literal["assistant"]]
    content: typing_extensions.Required[str]
    name: typing_extensions.NotRequired[str]


class _KensaAssistantToolCallMessage(typing_extensions.TypedDict, total=False):
    """Assistant tool-call message in Kensa's portable chat-completion subset."""

    role: typing_extensions.Required[Literal["assistant"]]
    content: typing_extensions.NotRequired[str | None]
    name: typing_extensions.NotRequired[str]
    tool_calls: typing_extensions.Required[list[KensaToolCall]]


KensaAssistantMessage: TypeAlias = _KensaAssistantTextMessage | _KensaAssistantToolCallMessage


class KensaToolMessage(typing_extensions.TypedDict):
    """Tool result message linked to an assistant tool call."""

    role: Literal["tool"]
    tool_call_id: str
    content: str


KensaMessage: TypeAlias = (
    KensaDeveloperMessage
    | KensaSystemMessage
    | KensaUserMessage
    | KensaAssistantMessage
    | KensaToolMessage
)


@dataclass(frozen=True)
class KensaCase:
    """Immutable case data used directly in pytest parametrization."""

    id: str
    row: Mapping[str, Any]
    _input: Any = _MISSING

    @property
    def input(self) -> Any:
        if self._input is not _MISSING:
            return self._input
        if "input" in self.row:
            return self.row["input"]
        if "messages" in self.row:
            return self.row["messages"]
        payload = {k: v for k, v in self.row.items() if k != "id"}
        if len(payload) == 1:
            return next(iter(payload.values()))
        return payload

    @property
    def messages(self) -> list[KensaMessage]:
        """Return messages provided through ``kensa_case(messages=...)``."""

        messages = self.row.get("messages")
        if isinstance(messages, list):
            return cast(list[KensaMessage], messages)
        raise KensaCaseError("case.messages is only available when messages=... was provided")

    @overload
    def run(
        self,
        agent: ConversationAgent,
        *,
        simulator: Simulator,
        max_turns: int | None = None,
        starts_with: Literal["simulator", "agent"] | None = None,
    ) -> Awaitable[CaseResult]: ...

    @overload
    def run(
        self,
        agent: _SyncConversationAgent,
        *,
        simulator: None = None,
        max_turns: None = None,
        starts_with: None = None,
    ) -> CaseResult: ...

    @overload
    def run(
        self,
        agent: _AsyncConversationAgent,
        *,
        simulator: None = None,
        max_turns: None = None,
        starts_with: None = None,
    ) -> Awaitable[CaseResult]: ...

    @overload
    def run(
        self,
        agent: ConversationAgent,
        *,
        simulator: None = None,
        max_turns: None = None,
        starts_with: None = None,
    ) -> CaseResult | Awaitable[CaseResult]: ...

    def run(
        self,
        agent: ConversationAgent,
        *,
        simulator: Simulator | None = None,
        max_turns: int | None = None,
        starts_with: Literal["simulator", "agent"] | None = None,
    ) -> CaseResult | Awaitable[CaseResult]:
        """Run this case through one conversation agent and optional simulator."""

        from kensa.conversation import _run_conversation

        def _run() -> CaseResult | Awaitable[CaseResult]:
            return _run_conversation(
                self,
                agent,
                simulator=simulator,
                max_turns=max_turns,
                starts_with=starts_with,
            )

        runtime = current_runtime()
        if runtime is not None:
            return runtime.run_case(self, _run)
        return _run()

    def __repr__(self) -> str:
        return self.id


def kensa_case(
    *,
    id: str,
    input: Any = _MISSING,
    messages: list[KensaMessage] | None = None,
    **fields: Any,
) -> KensaCase:
    """Create immutable case data for inline pytest parametrization."""

    if not id:
        raise KensaCaseError("kensa_case(id=...) requires a non-empty id")
    if input is not _MISSING and messages is not None:
        raise KensaCaseError("Use either input=... or messages=..., not both")
    if messages is not None:
        _validate_messages(messages)

    row: dict[str, Any] = {"id": id}
    row.update(fields)
    if input is not _MISSING:
        row["input"] = input
    if messages is not None:
        row["messages"] = messages

    resolved_input = input
    if resolved_input is _MISSING and messages is not None:
        resolved_input = messages
    return KensaCase(id=id, row=MappingProxyType(row), _input=resolved_input)


def _validate_messages(messages: list[KensaMessage]) -> None:
    if not messages:
        raise KensaCaseError("kensa_case(messages=...) requires at least one message")

    pending_tool_calls: set[str] = set()
    for index, message in enumerate(cast(list[Mapping[str, Any]], messages)):
        role = message.get("role")
        if role == "tool":
            _validate_tool_message(message, pending_tool_calls, index)
            continue
        if pending_tool_calls:
            raise KensaCaseError("assistant tool_calls must be followed by matching tool messages")
        if role in {"developer", "system", "user"}:
            _validate_text_message(message, role, index)
            continue
        if role == "assistant":
            _validate_assistant_message(message, pending_tool_calls, index)
            continue
        raise KensaCaseError(
            f"messages[{index}].role must be developer, system, user, assistant, or tool"
        )

    if pending_tool_calls:
        raise KensaCaseError("assistant tool_calls must be followed by matching tool messages")


def _validate_text_message(message: Mapping[str, Any], role: object, index: int) -> None:
    _reject_unknown_keys(message, {"role", "content", "name"}, f"messages[{index}]")
    if not isinstance(message.get("content"), str):
        raise KensaCaseError(f"messages[{index}].content must be a string for role {role!r}")
    _validate_optional_name(message, index)


def _validate_assistant_message(
    message: Mapping[str, Any], pending_tool_calls: set[str], index: int
) -> None:
    _reject_unknown_keys(message, {"role", "content", "name", "tool_calls"}, f"messages[{index}]")
    content = message.get("content")
    tool_calls = message.get("tool_calls")
    if tool_calls is None:
        if not isinstance(content, str):
            raise KensaCaseError(
                "assistant messages require string content unless tool_calls are present"
            )
    else:
        if content is not None and not isinstance(content, str):
            raise KensaCaseError("assistant message content must be a string or None")
        _validate_tool_calls(tool_calls, pending_tool_calls, index)
    _validate_optional_name(message, index)


def _validate_tool_calls(value: Any, pending_tool_calls: set[str], index: int) -> None:
    if not isinstance(value, list) or not value:
        raise KensaCaseError("assistant tool_calls must be a non-empty list")
    for tool_index, tool_call in enumerate(value):
        if not isinstance(tool_call, dict):
            raise KensaCaseError(f"messages[{index}].tool_calls[{tool_index}] must be an object")
        tool_call = cast(Mapping[str, Any], tool_call)
        _reject_unknown_keys(
            tool_call,
            {"id", "type", "function"},
            f"messages[{index}].tool_calls[{tool_index}]",
        )
        tool_call_id = tool_call.get("id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise KensaCaseError("assistant tool call id must be a non-empty string")
        if tool_call_id in pending_tool_calls:
            raise KensaCaseError(f"duplicate assistant tool_call id: {tool_call_id}")
        if tool_call.get("type") != "function":
            raise KensaCaseError("assistant tool_calls only support type='function'")
        _validate_function_call(tool_call.get("function"), index, tool_index)
        pending_tool_calls.add(tool_call_id)


def _validate_function_call(value: Any, index: int, tool_index: int) -> None:
    if not isinstance(value, dict):
        raise KensaCaseError(
            f"messages[{index}].tool_calls[{tool_index}].function must be an object"
        )
    _reject_unknown_keys(
        value,
        {"name", "arguments"},
        f"messages[{index}].tool_calls[{tool_index}].function",
    )
    if not isinstance(value.get("name"), str) or not value["name"]:
        raise KensaCaseError("assistant tool function name must be a non-empty string")
    arguments = value.get("arguments")
    if not isinstance(arguments, str):
        raise KensaCaseError("assistant tool function arguments must be a JSON object string")
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise KensaCaseError("assistant tool function arguments must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise KensaCaseError("assistant tool function arguments must be a JSON object string")


def _validate_tool_message(
    message: Mapping[str, Any], pending_tool_calls: set[str], index: int
) -> None:
    _reject_unknown_keys(message, {"role", "tool_call_id", "content"}, f"messages[{index}]")
    tool_call_id = message.get("tool_call_id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise KensaCaseError("tool messages require a non-empty tool_call_id")
    if tool_call_id not in pending_tool_calls:
        raise KensaCaseError(f"tool message references unknown tool_call_id: {tool_call_id}")
    if not isinstance(message.get("content"), str):
        raise KensaCaseError("tool message content must be a string")
    pending_tool_calls.remove(tool_call_id)


def _validate_optional_name(message: Mapping[str, Any], index: int) -> None:
    if "name" in message and not isinstance(message["name"], str):
        raise KensaCaseError(f"messages[{index}].name must be a string")


def _reject_unknown_keys(message: Mapping[str, Any], allowed: set[str], path: str) -> None:
    extra = sorted(set(message) - allowed)
    if extra:
        raise KensaCaseError(f"{path} contains unsupported keys: {', '.join(extra)}")


__all__ = [
    "KensaAssistantMessage",
    "KensaCase",
    "KensaCaseError",
    "KensaDeveloperMessage",
    "KensaFunctionCall",
    "KensaMessage",
    "KensaSystemMessage",
    "KensaToolCall",
    "KensaToolMessage",
    "KensaUserMessage",
    "kensa_case",
]
