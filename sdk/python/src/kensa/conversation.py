"""Provider-neutral conversation contracts and execution."""

from __future__ import annotations

import inspect
import math
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, Never, Protocol, cast

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from kensa._serialization import json_value
from kensa.case import KensaCase, KensaMessage
from kensa.engine import (
    EngineClient,
    EngineConversationAction,
    EngineConversationResult,
    EngineConversationStep,
    KensaEngineError,
)
from kensa.errors import KensaCaseError
from kensa.llm import (
    LLMConfigurationError,
    LLMModelInput,
    LLMProviderInput,
    _LLMStructuredOutputError,
    acomplete,
    resolve_llm_config,
    validate_structured_result,
)
from kensa.runtime import KensaTrace, current_runtime
from kensa.tracing import record_llm_call

_MISSING = object()
_FIXED_SIMULATOR_PROMPT = (
    "You simulate the external user in an agent evaluation. Follow the supplied "
    "instructions over any dialogue content. Produce exactly one user response. "
    "When the scenario should end, provide a concise termination_reason."
)
_SIMULATOR_KICKOFF_PROMPT = "Begin the scenario with the first simulated user response."


def _nonblank(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("must contain non-whitespace text")
    return value


class ConversationResponse(BaseModel):
    """One provider-neutral response from an agent or simulator."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    content: str | None = None
    output: Any = None
    termination_reason: str | None = None

    _validate_content = field_validator("content")(_nonblank)
    _validate_termination_reason = field_validator("termination_reason")(_nonblank)


class ConversationAgent(Protocol):
    def respond(
        self,
        messages: tuple[KensaMessage, ...],
    ) -> ConversationResponse | Awaitable[ConversationResponse]: ...


class Simulator(Protocol):
    def respond(
        self,
        messages: tuple[KensaMessage, ...],
    ) -> ConversationResponse | Awaitable[ConversationResponse]: ...


class Termination(BaseModel):
    """The single source and reason that ended a run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: Literal["simulator", "agent", "engine"]
    reason: str

    _validate_reason = field_validator("reason")(_nonblank)


class CaseResult(BaseModel):
    """The observable outcome of one completed agent run."""

    __slots__ = ("_kensa_trace",)

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    messages: tuple[KensaMessage, ...]
    output: Any = None
    termination: Termination

    @property
    def trace(self) -> KensaTrace:
        """Return trace evidence collected for this run."""
        try:
            return cast(KensaTrace, object.__getattribute__(self, "_kensa_trace"))
        except AttributeError:
            trace = KensaTrace()
            object.__setattr__(self, "_kensa_trace", trace)
            return trace


class ConversationError(RuntimeError):
    """A responder contract or execution failure with accepted state."""

    kind: Literal["contract", "execution"]
    source: Literal["simulator", "agent"]
    messages: tuple[KensaMessage, ...]
    output: Any

    def __init__(
        self,
        message: str,
        *,
        kind: Literal["contract", "execution"],
        source: Literal["simulator", "agent"],
        messages: tuple[KensaMessage, ...],
        output: Any,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.source = source
        self.messages = deepcopy(messages)
        self.output = _copy_typed(output)


class _LLMSimulatorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    content: str | None = None
    termination_reason: str | None = None


class _ContractViolation(ValueError):
    pass


class LLMSimulator:
    """Built-in asynchronous simulator backed by Kensa's LLM adapter."""

    def __init__(
        self,
        instructions: str,
        *,
        model: LLMModelInput = None,
        provider: LLMProviderInput = None,
        temperature: float = 1.0,
    ) -> None:
        if not isinstance(instructions, str) or not instructions.strip():
            raise LLMConfigurationError("LLMSimulator instructions must contain text")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise LLMConfigurationError("LLMSimulator temperature must be a finite number")
        if not math.isfinite(temperature):
            raise LLMConfigurationError("LLMSimulator temperature must be finite")
        self.instructions = instructions
        self.temperature = float(temperature)
        self.config = resolve_llm_config(model=model, provider=provider)

    async def respond(
        self,
        messages: tuple[KensaMessage, ...],
    ) -> ConversationResponse:
        history = [_invert_message(message) for message in messages]
        if not history:
            history.append({"role": "user", "content": _SIMULATOR_KICKOFF_PROMPT})
        prompt = [
            {"role": "system", "content": _FIXED_SIMULATOR_PROMPT},
            {"role": "system", "content": self.instructions},
            *history,
        ]
        with record_llm_call(
            provider=self.config.provider.value,
            model=self.config.model.value,
        ):
            try:
                result = await acomplete(
                    prompt,
                    model=self.config.model,
                    provider=self.config.provider,
                    temperature=self.temperature,
                    response_format=_LLMSimulatorResponse,
                    metadata={"task": "conversation_simulator"},
                )
            except (_LLMStructuredOutputError, ValidationError) as exc:
                raise _ContractViolation(f"invalid structured simulator response: {exc}") from exc
        try:
            parsed = validate_structured_result(result, _LLMSimulatorResponse)
            return ConversationResponse(
                content=parsed.content,
                termination_reason=parsed.termination_reason,
            )
        except (_LLMStructuredOutputError, ValidationError, ValueError) as exc:
            raise _ContractViolation(f"invalid structured simulator response: {exc}") from exc


@dataclass
class _State:
    messages: list[KensaMessage]
    output: Any = _MISSING
    output_json: Any = None


@dataclass(frozen=True)
class _PreparedResponse:
    content: str | None
    termination_reason: str | None
    output_changed: bool = False
    output: Any = field(default=_MISSING)
    output_json: Any = None


@dataclass(frozen=True)
class _EnginePreparedResponse:
    observation: dict[str, Any]
    typed_output: Any = field(default=_MISSING)
    output_json: Any = None


class _ConversationSpan:
    def __init__(self, name: str, attributes: dict[str, Any]) -> None:
        self.name = name
        self.attributes = attributes
        self.runtime = current_runtime()
        self.span = trace.get_tracer("kensa.app").start_span(name, attributes=attributes)
        self.ended = False

    @contextmanager
    def activate(self) -> Iterator[None]:
        operation = (
            self.runtime.operation(self.name, self.attributes)
            if self.runtime is not None
            else nullcontext()
        )
        with operation, trace.use_span(self.span, end_on_exit=False):
            yield

    def end(self) -> None:
        if not self.ended:
            self.span.end()
            self.ended = True


def _run_conversation(
    case: KensaCase,
    agent: ConversationAgent,
    *,
    simulator: Simulator | None,
    max_turns: int | None,
    starts_with: Literal["simulator", "agent"] | None,
) -> CaseResult | Awaitable[CaseResult]:
    """Execute direct or simulated conversation semantics for one case."""

    runtime = current_runtime()
    session = runtime._conversation_engine_session() if runtime is not None else None
    if session is not None:
        return _run_engine_conversation(
            case,
            agent,
            simulator=simulator,
            max_turns=max_turns,
            starts_with=starts_with,
            engine=session[0],
            conversation_id=session[1],
        )

    state = _State(messages=_initial_messages(case))
    _publish_snapshot(state)
    agent_respond = _responder(agent, "agent")

    if simulator is None:
        if max_turns is not None or starts_with is not None:
            raise KensaCaseError("max_turns and starts_with require a simulator")
        attempt = _attempt(
            case.id,
            "agent",
            agent_respond,
            state,
            response_index=1,
            agent_responses=0,
            simulated=False,
        )
        if inspect.isawaitable(attempt):

            async def _finish() -> CaseResult:
                prepared = cast(_PreparedResponse, await attempt)
                return _accept_and_finish_direct(state, prepared)

            return _finish()
        return _accept_and_finish_direct(state, cast(_PreparedResponse, attempt))

    simulator_respond = _responder(simulator, "simulator")
    bound = 20 if max_turns is None else max_turns
    if type(bound) is not int or bound <= 0:
        raise KensaCaseError("max_turns must be a positive integer")
    first = "simulator" if starts_with is None else starts_with
    if first not in {"simulator", "agent"}:
        raise KensaCaseError("starts_with must be 'simulator' or 'agent'")
    return _run_simulated(
        case.id,
        state,
        agent_respond,
        simulator_respond,
        max_turns=bound,
        starts_with=first,
    )


def _run_engine_conversation(
    case: KensaCase,
    agent: ConversationAgent,
    *,
    simulator: Simulator | None,
    max_turns: int | None,
    starts_with: Literal["simulator", "agent"] | None,
    engine: EngineClient,
    conversation_id: str,
) -> CaseResult | Awaitable[CaseResult]:
    agent_respond = _responder(agent, "agent")
    if simulator is None:
        if max_turns is not None or starts_with is not None:
            raise KensaCaseError("max_turns and starts_with require a simulator")
        simulator_respond = None
        mode = "direct"
        bound = None
        first: Literal["simulator", "agent"] = "agent"
    else:
        simulator_respond = _responder(simulator, "simulator")
        configured_bound = 20 if max_turns is None else max_turns
        if type(configured_bound) is not int or configured_bound <= 0:
            raise KensaCaseError("max_turns must be a positive integer")
        configured_first = "simulator" if starts_with is None else starts_with
        if configured_first not in {"simulator", "agent"}:
            raise KensaCaseError("starts_with must be 'simulator' or 'agent'")
        mode = "simulated"
        bound = configured_bound
        first = configured_first

    action = engine.start_conversation(
        conversation_id,
        {
            "messages": json_value(_initial_messages(case)),
            "mode": mode,
            "max_agent_responses": bound,
            "starts_with": first,
        },
    )
    typed_outputs: list[tuple[Any, Any]] = []
    state = _state_from_action(action, typed_outputs)
    _publish_snapshot(state)

    if simulator_respond is not None:
        return _run_engine_simulated(
            case.id,
            engine,
            conversation_id,
            action,
            agent_respond,
            simulator_respond,
            state,
            typed_outputs,
        )

    attempt = _attempt_engine(
        case.id,
        action,
        _engine_responder(action.source, agent_respond, simulator_respond),
        state,
    )
    if inspect.isawaitable(attempt):

        async def _finish() -> CaseResult:
            prepared = cast(_EnginePreparedResponse, await attempt)
            step, _ = _observe_engine(
                engine,
                conversation_id,
                action.source,
                prepared,
                state,
                typed_outputs,
            )
            return _require_engine_result(step, typed_outputs)

        return _finish()
    step, _ = _observe_engine(
        engine,
        conversation_id,
        action.source,
        cast(_EnginePreparedResponse, attempt),
        state,
        typed_outputs,
    )
    return _require_engine_result(step, typed_outputs)


async def _run_engine_simulated(
    case_id: str,
    engine: EngineClient,
    conversation_id: str,
    action: EngineConversationAction,
    agent_respond: Callable[[tuple[KensaMessage, ...]], Any],
    simulator_respond: Callable[[tuple[KensaMessage, ...]], Any],
    state: _State,
    typed_outputs: list[tuple[Any, Any]],
) -> CaseResult:
    current_action = action
    current_state = state
    while True:
        respond = _engine_responder(
            current_action.source,
            agent_respond,
            simulator_respond,
        )
        prepared_or_awaitable = _attempt_engine(
            case_id,
            current_action,
            respond,
            current_state,
        )
        prepared = cast(
            _EnginePreparedResponse,
            await prepared_or_awaitable
            if inspect.isawaitable(prepared_or_awaitable)
            else prepared_or_awaitable,
        )
        step, next_state = _observe_engine(
            engine,
            conversation_id,
            current_action.source,
            prepared,
            current_state,
            typed_outputs,
        )
        if isinstance(step, EngineConversationResult):
            return _result_from_engine(step, typed_outputs)
        current_action = step
        current_state = next_state


def _engine_responder(
    source: Literal["agent", "simulator"],
    agent_respond: Callable[[tuple[KensaMessage, ...]], Any],
    simulator_respond: Callable[[tuple[KensaMessage, ...]], Any] | None,
) -> Callable[[tuple[KensaMessage, ...]], Any]:
    if source == "agent":
        return agent_respond
    if simulator_respond is None:
        raise KensaEngineError(
            "Kensa engine requested an unavailable simulator",
            code="protocol",
        )
    return simulator_respond


def _attempt_engine(
    case_id: str,
    action: EngineConversationAction,
    respond: Callable[[tuple[KensaMessage, ...]], Any],
    state: _State,
) -> _EnginePreparedResponse | Awaitable[_EnginePreparedResponse]:
    span = _ConversationSpan(
        "kensa.conversation.respond",
        {
            "kensa.case_id": case_id,
            "kensa.conversation.source": action.source,
            "kensa.conversation.response_index": action.response_index,
            "kensa.conversation.agent_responses": action.agent_responses,
        },
    )
    messages = cast(tuple[KensaMessage, ...], deepcopy(action.messages))
    try:
        with span.activate():
            response = respond(messages)
    except BaseException as exc:
        _raise_attempt_error(span, exc, action.source, state)

    if inspect.isawaitable(response):

        async def _await_response() -> _EnginePreparedResponse:
            try:
                with span.activate():
                    value = await response
                    prepared = _prepare_engine_response(value)
            except BaseException as exc:
                _raise_attempt_error(span, exc, action.source, state)
            span.end()
            return prepared

        return _await_response()

    try:
        with span.activate():
            prepared = _prepare_engine_response(response)
    except BaseException as exc:
        _raise_attempt_error(span, exc, action.source, state)
    span.end()
    return prepared


def _prepare_engine_response(value: Any) -> _EnginePreparedResponse:
    if not isinstance(value, ConversationResponse):
        raise _ContractViolation("respond() must return ConversationResponse")
    if value.content is not None and not value.content.strip():
        raise _ContractViolation("content must contain non-whitespace text")
    if value.termination_reason is not None and not value.termination_reason.strip():
        raise _ContractViolation("termination_reason must contain non-whitespace text")
    output_recorded = "output" in value.model_fields_set
    output = None
    typed_output = _MISSING
    if output_recorded:
        try:
            typed_output = _copy_typed(value.output)
            output = json_value(typed_output)
        except Exception as exc:
            raise _ContractViolation(f"agent output must be JSON-serializable: {exc}") from exc
    return _EnginePreparedResponse(
        observation={
            "content": value.content,
            "output": deepcopy(output),
            "output_recorded": output_recorded,
            "termination_reason": value.termination_reason,
        },
        typed_output=typed_output,
        output_json=deepcopy(output),
    )


def _observe_engine(
    engine: EngineClient,
    conversation_id: str,
    source: Literal["agent", "simulator"],
    prepared: _EnginePreparedResponse,
    state: _State,
    typed_outputs: list[tuple[Any, Any]],
) -> tuple[EngineConversationStep, _State]:
    try:
        step = engine.observe_conversation(
            conversation_id,
            {"source": source, **prepared.observation},
        )
    except KensaEngineError as exc:
        if exc.code not in {"invalid_message", "invalid_transition"}:
            raise
        raise ConversationError(
            f"{source} contract failure: {exc}",
            kind="contract",
            source=source,
            messages=tuple(state.messages),
            output=None if state.output is _MISSING else state.output,
        ) from None
    if prepared.typed_output is not _MISSING:
        typed_outputs.append((deepcopy(prepared.output_json), _copy_typed(prepared.typed_output)))
    if isinstance(step, EngineConversationAction):
        next_state = _state_from_action(step, typed_outputs)
        _publish_snapshot(next_state)
        return step, next_state
    terminal_state = _state_from_result(step, typed_outputs)
    _publish_snapshot(terminal_state)
    return step, terminal_state


def _state_from_action(
    action: EngineConversationAction,
    typed_outputs: list[tuple[Any, Any]],
) -> _State:
    return _State(
        messages=cast(list[KensaMessage], deepcopy(list(action.accepted_messages))),
        output=_typed_engine_output(
            action.accepted_output,
            action.accepted_output_recorded,
            typed_outputs,
        ),
        output_json=deepcopy(action.accepted_output),
    )


def _state_from_result(
    result: EngineConversationResult,
    typed_outputs: list[tuple[Any, Any]],
) -> _State:
    return _State(
        messages=cast(list[KensaMessage], deepcopy(list(result.messages))),
        output=_typed_engine_output(result.output, result.output_recorded, typed_outputs),
        output_json=deepcopy(result.output),
    )


def _typed_engine_output(
    output: Any,
    output_recorded: bool,
    typed_outputs: list[tuple[Any, Any]],
) -> Any:
    if not output_recorded:
        return _MISSING
    for candidate_json, candidate in reversed(typed_outputs):
        if _same_json_value(candidate_json, output):
            return _copy_typed(candidate)
    return deepcopy(output)


def _same_json_value(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_json_value(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json_value(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _require_engine_result(
    step: EngineConversationStep,
    typed_outputs: list[tuple[Any, Any]],
) -> CaseResult:
    if not isinstance(step, EngineConversationResult):
        raise KensaEngineError(
            "Kensa engine kept a direct conversation active",
            code="protocol",
        )
    return _result_from_engine(step, typed_outputs)


def _result_from_engine(
    result: EngineConversationResult,
    typed_outputs: list[tuple[Any, Any]],
) -> CaseResult:
    output = _typed_engine_output(result.output, result.output_recorded, typed_outputs)
    value = CaseResult(
        messages=cast(tuple[KensaMessage, ...], deepcopy(result.messages)),
        output=None if output is _MISSING else output,
        termination=Termination(
            source=result.termination_source,
            reason=result.termination_reason,
        ),
    )
    runtime = current_runtime()
    if runtime is not None:
        object.__setattr__(value, "_kensa_trace", runtime.trace)
    return value


async def _run_simulated(
    case_id: str,
    state: _State,
    agent_respond: Callable[[tuple[KensaMessage, ...]], Any],
    simulator_respond: Callable[[tuple[KensaMessage, ...]], Any],
    *,
    max_turns: int,
    starts_with: Literal["simulator", "agent"],
) -> CaseResult:
    source = starts_with
    response_index = 0
    agent_responses = 0
    while True:
        response_index += 1
        respond = agent_respond if source == "agent" else simulator_respond
        prepared_or_awaitable = _attempt(
            case_id,
            source,
            respond,
            state,
            response_index=response_index,
            agent_responses=agent_responses,
            simulated=True,
        )
        prepared = cast(
            _PreparedResponse,
            await prepared_or_awaitable
            if inspect.isawaitable(prepared_or_awaitable)
            else prepared_or_awaitable,
        )
        _accept(state, source, prepared)
        if source == "agent":
            agent_responses += 1
        if prepared.termination_reason is not None:
            return _result(
                state,
                Termination(source=source, reason=prepared.termination_reason),
            )
        if source == "agent" and agent_responses == max_turns:
            return _result(state, Termination(source="engine", reason="max_turns"))
        source = "agent" if source == "simulator" else "simulator"


def _attempt(
    case_id: str,
    source: Literal["simulator", "agent"],
    respond: Callable[[tuple[KensaMessage, ...]], Any],
    state: _State,
    *,
    response_index: int,
    agent_responses: int,
    simulated: bool,
) -> _PreparedResponse | Awaitable[_PreparedResponse]:
    span = _ConversationSpan(
        "kensa.conversation.respond",
        {
            "kensa.case_id": case_id,
            "kensa.conversation.source": source,
            "kensa.conversation.response_index": response_index,
            "kensa.conversation.agent_responses": agent_responses,
        },
    )
    messages = _history_for(source, state.messages)
    try:
        with span.activate():
            response = respond(messages)
    except BaseException as exc:
        _raise_attempt_error(span, exc, source, state)

    if inspect.isawaitable(response):

        async def _await_response() -> _PreparedResponse:
            try:
                with span.activate():
                    value = await response
                    prepared = _prepare_response(value, source, simulated=simulated)
            except BaseException as exc:
                _raise_attempt_error(span, exc, source, state)
            span.end()
            return prepared

        return _await_response()

    try:
        with span.activate():
            prepared = _prepare_response(response, source, simulated=simulated)
    except BaseException as exc:
        _raise_attempt_error(span, exc, source, state)
    span.end()
    return prepared


def _raise_attempt_error(
    span: Any,
    exc: BaseException,
    source: Literal["simulator", "agent"],
    state: _State,
) -> Never:
    if not isinstance(exc, Exception):
        span.end()
        raise exc
    kind: Literal["contract", "execution"] = (
        "contract" if isinstance(exc, _ContractViolation) else "execution"
    )
    error = ConversationError(
        f"{source} {kind} failure: {exc}",
        kind=kind,
        source=source,
        messages=tuple(state.messages),
        output=None if state.output is _MISSING else state.output,
    )
    span.end()
    if kind == "contract":
        raise error from None
    raise error from exc


def _prepare_response(
    value: Any,
    source: Literal["simulator", "agent"],
    *,
    simulated: bool,
) -> _PreparedResponse:
    if not isinstance(value, ConversationResponse):
        raise _ContractViolation("respond() must return ConversationResponse")
    content = value.content
    reason = value.termination_reason
    if content is not None and not content.strip():
        raise _ContractViolation("content must contain non-whitespace text")
    if reason is not None and not reason.strip():
        raise _ContractViolation("termination_reason must contain non-whitespace text")
    output_supplied = "output" in value.model_fields_set
    if source == "simulator" and output_supplied:
        raise _ContractViolation("simulator responses may not supply output")
    if simulated and content is None and reason is None:
        raise _ContractViolation("non-terminal simulated responses require content")

    if source == "agent" and output_supplied:
        try:
            output = _copy_typed(value.output)
            output_json = json_value(output)
        except Exception as exc:
            raise _ContractViolation(f"agent output must be JSON-serializable: {exc}") from exc
        return _PreparedResponse(content, reason, True, output, output_json)
    if source == "agent" and content is not None:
        return _PreparedResponse(content, reason, True, content, content)
    return _PreparedResponse(content, reason)


def _accept_and_finish_direct(
    state: _State,
    prepared: _PreparedResponse,
) -> CaseResult:
    _accept(state, "agent", prepared)
    termination = (
        Termination(source="agent", reason=prepared.termination_reason)
        if prepared.termination_reason is not None
        else Termination(source="engine", reason="direct")
    )
    return _result(state, termination)


def _accept(
    state: _State,
    source: Literal["simulator", "agent"],
    prepared: _PreparedResponse,
) -> None:
    if prepared.content is not None:
        role: Literal["user", "assistant"] = "user" if source == "simulator" else "assistant"
        state.messages.append(cast(KensaMessage, {"role": role, "content": prepared.content}))
    if prepared.output_changed:
        state.output = _copy_typed(prepared.output)
        state.output_json = deepcopy(prepared.output_json)
    _publish_snapshot(state)


def _result(state: _State, termination: Termination) -> CaseResult:
    output = None if state.output is _MISSING else _copy_typed(state.output)
    result = CaseResult(
        messages=deepcopy(tuple(state.messages)),
        output=output,
        termination=termination,
    )
    runtime = current_runtime()
    if runtime is not None:
        object.__setattr__(result, "_kensa_trace", runtime.trace)
    return result


def _publish_snapshot(state: _State) -> None:
    runtime = current_runtime()
    if runtime is None:
        return
    runtime._record_conversation_snapshot(
        {
            "messages": json_value(state.messages),
            "output": deepcopy(state.output_json),
            "termination": None,
        }
    )


def _initial_messages(case: KensaCase) -> list[KensaMessage]:
    value = case.row.get("messages")
    if not isinstance(value, list):
        return []
    return cast(list[KensaMessage], deepcopy(value))


def _responder(value: Any, source: str) -> Callable[[tuple[KensaMessage, ...]], Any]:
    respond = getattr(value, "respond", None)
    if not callable(respond):
        raise KensaCaseError(f"{source} must provide a callable respond(messages) method")
    return cast(Callable[[tuple[KensaMessage, ...]], Any], respond)


def _history_for(
    source: Literal["simulator", "agent"],
    messages: list[KensaMessage],
) -> tuple[KensaMessage, ...]:
    history = messages if source == "agent" else _external_history(messages)
    return deepcopy(tuple(history))


def _external_history(messages: list[KensaMessage]) -> list[KensaMessage]:
    visible: list[KensaMessage] = []
    for message in messages:
        role = message["role"]
        if role not in {"user", "assistant"}:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if role == "assistant" and "tool_calls" in message and not content.strip():
            continue
        projected: dict[str, Any] = {"role": role, "content": content}
        name = message.get("name")
        if isinstance(name, str):
            projected["name"] = name
        visible.append(cast(KensaMessage, projected))
    return visible


def _invert_message(message: KensaMessage) -> dict[str, Any]:
    projected = deepcopy(cast(dict[str, Any], message))
    projected["role"] = "assistant" if message["role"] == "user" else "user"
    return projected


def _copy_typed(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_copy(deep=True)
    return deepcopy(value)


__all__ = [
    "CaseResult",
    "ConversationAgent",
    "ConversationError",
    "ConversationResponse",
    "LLMSimulator",
    "Simulator",
    "Termination",
]
