from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Awaitable
from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest
from pydantic import BaseModel, ValidationError

import kensa.conversation as conversation
from kensa.case import KensaCaseError, KensaMessage, kensa_case
from kensa.conversation import (
    CaseResult,
    ConversationAgent,
    ConversationError,
    ConversationResponse,
    LLMSimulator,
    Simulator,
    Termination,
)
from kensa.engine import (
    EngineClient,
    EngineConversationAction,
    EngineConversationResult,
    KensaEngineError,
)
from kensa.llm import LLMConfigurationError, LLMProviderError, LLMResult
from kensa.runtime import KensaTrial, KensaTrialRuntime, reset_current_runtime, set_current_runtime


class Value(BaseModel):
    items: list[int]


class ScriptedResponder:
    def __init__(self, *responses: ConversationResponse | BaseException | object) -> None:
        self.responses = list(responses)
        self.histories: list[tuple[KensaMessage, ...]] = []
        self.calls = 0

    def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
        self.calls += 1
        self.histories.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return cast(ConversationResponse, response)


class ScriptedConversationEngine:
    def __init__(
        self,
        first: EngineConversationAction,
        *steps: EngineConversationAction | EngineConversationResult | KensaEngineError,
    ) -> None:
        self.first = first
        self.steps = list(steps)
        self.starts: list[tuple[str, dict[str, Any]]] = []
        self.observations: list[tuple[str, dict[str, Any]]] = []

    def start_case(self, evaluation_id: str, case: dict[str, Any]) -> None:
        del evaluation_id, case

    def start_conversation(
        self,
        conversation_id: str,
        conversation: dict[str, Any],
    ) -> EngineConversationAction:
        self.starts.append((conversation_id, deepcopy(conversation)))
        return self.first

    def observe_conversation(
        self,
        conversation_id: str,
        observation: dict[str, Any],
    ) -> EngineConversationAction | EngineConversationResult:
        self.observations.append((conversation_id, deepcopy(observation)))
        step = self.steps.pop(0)
        if isinstance(step, KensaEngineError):
            raise step
        return step


def engine_action(
    source: Literal["agent", "simulator"],
    *,
    messages: tuple[dict[str, Any], ...] = (),
    accepted_messages: tuple[dict[str, Any], ...] = (),
    accepted_output: Any = None,
    accepted_output_recorded: bool = False,
    response_index: int = 1,
    agent_responses: int = 0,
) -> EngineConversationAction:
    return EngineConversationAction(
        source=source,
        messages=messages,
        response_index=response_index,
        agent_responses=agent_responses,
        accepted_messages=accepted_messages,
        accepted_output=accepted_output,
        accepted_output_recorded=accepted_output_recorded,
    )


def engine_runtime(engine: ScriptedConversationEngine, nodeid: str) -> KensaTrialRuntime:
    return KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid=nodeid,
        group_id="group",
        case_id="case",
        no_judge=False,
        engine=cast(Any, engine),
    )


if TYPE_CHECKING:
    from typing import assert_type

    class _StaticSyncAgent:
        def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            return ConversationResponse()

    class _StaticAsyncAgent:
        async def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            return ConversationResponse()

    class _StaticUnionAgent:
        def respond(
            self,
            messages: tuple[KensaMessage, ...],
        ) -> ConversationResponse | Awaitable[ConversationResponse]:
            return ConversationResponse()

    class _StaticSimulator:
        def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            return ConversationResponse(termination_reason="done")

    _static_case = kensa_case(id="typing", input="x")
    assert_type(_static_case.run(_StaticSyncAgent()), CaseResult)
    assert_type(_static_case.run(_StaticAsyncAgent()), Awaitable[CaseResult])
    assert_type(
        _static_case.run(_StaticUnionAgent()),
        CaseResult | Awaitable[CaseResult],
    )
    assert_type(
        _static_case.run(_StaticSyncAgent(), simulator=_StaticSimulator()),
        Awaitable[CaseResult],
    )


def test_public_conversation_contract_is_minimal_and_provider_neutral() -> None:
    assert conversation.__all__ == [
        "CaseResult",
        "ConversationAgent",
        "ConversationError",
        "ConversationResponse",
        "LLMSimulator",
        "Simulator",
        "Termination",
    ]
    assert ConversationAgent is not Simulator
    assert "openai" not in inspect.getsource(conversation).lower()
    assert "anthropic" not in inspect.getsource(conversation).lower()

    messages: tuple[KensaMessage, ...] = (
        {"role": "developer", "content": "developer"},
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user", "name": "customer"},
        {"role": "assistant", "content": "assistant"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    )
    result = CaseResult(
        messages=messages,
        output={"ok": True},
        termination=Termination(source="engine", reason="direct"),
    )
    assert result.messages == messages

    unmanaged = CaseResult(
        messages=(),
        termination=Termination(source="engine", reason="direct"),
    )
    equivalent = CaseResult(
        messages=(),
        termination=Termination(source="engine", reason="direct"),
    )
    assert unmanaged.trace is unmanaged.trace
    assert unmanaged.trace.spans == []
    assert not unmanaged.trace.incomplete
    assert "_kensa_trace" not in unmanaged.__dict__
    assert set(CaseResult.model_fields) == {"messages", "output", "termination"}
    assert unmanaged.model_dump() == {
        "messages": (),
        "output": None,
        "termination": {"source": "engine", "reason": "direct"},
    }
    assert unmanaged.model_dump_json() == (
        '{"messages":[],"output":null,"termination":{"source":"engine","reason":"direct"}}'
    )
    assert set(CaseResult.model_json_schema()["properties"]) == {
        "messages",
        "output",
        "termination",
    }
    assert repr(unmanaged) == (
        "CaseResult(messages=(), output=None, "
        "termination=Termination(source='engine', reason='direct'))"
    )
    unmanaged_hash = hash(unmanaged)
    unmanaged.trace.replace([], incomplete=True, incomplete_reason="partial")
    assert unmanaged == equivalent
    assert hash(unmanaged) == unmanaged_hash == hash(equivalent)
    assert not unmanaged.model_copy().trace.incomplete
    with pytest.raises(ValidationError, match="frozen"):
        cast(Any, unmanaged).trace = equivalent.trace

    for model, field in (
        (ConversationResponse, {"extra": True}),
        (Termination, {"source": "engine", "reason": "done", "extra": True}),
        (
            CaseResult,
            {
                "messages": (),
                "termination": Termination(source="engine", reason="done"),
                "extra": True,
            },
        ),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(field)

    with pytest.raises(ValidationError):
        ConversationResponse.model_validate({"content": 1})
    with pytest.raises(ValidationError):
        ConversationResponse(content=" ")
    with pytest.raises(ValidationError):
        ConversationResponse(termination_reason="\t")
    with pytest.raises(ValidationError):
        Termination(source="engine", reason=" ")


@pytest.mark.parametrize(
    ("response", "expected_messages", "expected_output", "expected_source", "expected_reason"),
    [
        (
            ConversationResponse(content="hello"),
            ({"role": "assistant", "content": "hello"},),
            "hello",
            "engine",
            "direct",
        ),
        (
            ConversationResponse(content="hello", output={"intent": "greet"}),
            ({"role": "assistant", "content": "hello"},),
            {"intent": "greet"},
            "engine",
            "direct",
        ),
        (
            ConversationResponse(content="hello", output=None),
            ({"role": "assistant", "content": "hello"},),
            None,
            "engine",
            "direct",
        ),
        (
            ConversationResponse(output={"status": "done"}),
            (),
            {"status": "done"},
            "engine",
            "direct",
        ),
        (ConversationResponse(), (), None, "engine", "direct"),
        (
            ConversationResponse(termination_reason="finished"),
            (),
            None,
            "agent",
            "finished",
        ),
    ],
)
def test_direct_mode_resolves_output_matrix(
    response: ConversationResponse,
    expected_messages: tuple[KensaMessage, ...],
    expected_output: Any,
    expected_source: str,
    expected_reason: str,
) -> None:
    agent = ScriptedResponder(response)

    result = kensa_case(id="direct", input="ignored").run(agent)

    assert isinstance(result, CaseResult)
    assert result.messages == expected_messages
    assert result.output == expected_output
    assert result.termination.source == expected_source
    assert result.termination.reason == expected_reason
    assert agent.calls == 1
    assert agent.histories == [()]


def test_agent_response_separates_content_from_typed_output_and_copies_values() -> None:
    source = Value(items=[1])
    response = ConversationResponse(content="visible", output=source)
    result = kensa_case(id="typed", input="x").run(ScriptedResponder(response))

    assert isinstance(result.output, Value)
    assert result.output is not source
    assert result.output.items == [1]
    source.items.append(2)
    cast(Value, result.output).items.append(3)
    assert response.output.items == [1, 2]

    class BrokenDump(BaseModel):
        value: str = "x"

        def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            del args, kwargs
            raise TypeError("broken dump")

    with pytest.raises(ConversationError) as raised:
        kensa_case(id="bad", input="x").run(
            ScriptedResponder(ConversationResponse(output=BrokenDump()))
        )
    assert raised.value.kind == "contract"
    assert raised.value.source == "agent"
    assert raised.value.messages == ()
    assert raised.value.output is None

    with pytest.raises(ConversationError, match="JSON"):
        kensa_case(id="bad", input="x").run(
            ScriptedResponder(ConversationResponse(output={"bad": object()}))
        )


@pytest.mark.asyncio
async def test_each_responder_receives_exact_isolated_history() -> None:
    initial: list[KensaMessage] = [
        {"role": "system", "content": "private system"},
        {"role": "developer", "content": "private developer"},
        {"role": "user", "content": ""},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "hello", "name": "customer"},
        {
            "role": "assistant",
            "content": "checking",
            "name": "support",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "private result"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "private_lookup", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_2", "content": "more private data"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_3",
                    "type": "function",
                    "function": {"name": "silent_lookup", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_3", "content": "silent private data"},
        {"role": "assistant", "content": "found it"},
    ]
    agent = ScriptedResponder(
        ConversationResponse(content="agent answer", termination_reason="done")
    )
    simulator = ScriptedResponder(ConversationResponse(content="customer follow-up"))

    result = await kensa_case(id="history", messages=initial).run(
        agent,
        simulator=simulator,
        max_turns=1,
    )

    assert agent.histories == [
        (
            *deepcopy(initial),
            {"role": "user", "content": "customer follow-up"},
        )
    ]
    assert simulator.histories == [
        (
            {"role": "user", "content": ""},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "hello", "name": "customer"},
            {"role": "assistant", "content": "checking", "name": "support"},
            {"role": "assistant", "content": "found it"},
        )
    ]
    assert result.messages == (
        *initial,
        {"role": "user", "content": "customer follow-up"},
        {"role": "assistant", "content": "agent answer"},
    )
    assert agent.histories[0] is not result.messages
    cast(dict[str, Any], simulator.histories[0][0])["content"] = "mutated"
    assert result.messages[4]["content"] == "hello"


@pytest.mark.asyncio
async def test_simulation_alternates_and_counts_only_agent_responses() -> None:
    agent = ScriptedResponder(
        ConversationResponse(content="a1"),
        ConversationResponse(content="a2"),
    )
    simulator = ScriptedResponder(
        ConversationResponse(content="s1"),
        ConversationResponse(content="s2"),
    )

    result = await kensa_case(id="alternate", input="x").run(
        agent,
        simulator=simulator,
        max_turns=2,
        starts_with="agent",
    )

    assert result.messages == (
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "s1"},
        {"role": "assistant", "content": "a2"},
    )
    assert result.output == "a2"
    assert result.termination == Termination(source="engine", reason="max_turns")
    assert agent.calls == 2
    assert simulator.calls == 1

    ending_agent = ScriptedResponder(
        ConversationResponse(content="final", termination_reason="resolved")
    )
    unused_simulator = ScriptedResponder(ConversationResponse(content="must not run"))
    final = await kensa_case(id="precedence", input="x").run(
        ending_agent,
        simulator=unused_simulator,
        max_turns=1,
        starts_with="agent",
    )
    assert final.termination == Termination(source="agent", reason="resolved")
    assert unused_simulator.calls == 0


@pytest.mark.asyncio
async def test_simulator_can_terminate_before_agent() -> None:
    agent = ScriptedResponder(ConversationResponse(content="must not run"))
    simulator = ScriptedResponder(
        ConversationResponse(content="goodbye", termination_reason="done")
    )

    result = await kensa_case(id="simulator_end", input="x").run(
        agent,
        simulator=simulator,
        max_turns=3,
    )

    assert result.messages == ({"role": "user", "content": "goodbye"},)
    assert result.output is None
    assert result.termination == Termination(source="simulator", reason="done")
    assert agent.calls == 0
    assert simulator.calls == 1


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"max_turns": 1}, "simulator"),
        ({"starts_with": "agent"}, "simulator"),
        ({"simulator": object()}, "respond"),
        ({"simulator": ScriptedResponder(), "max_turns": True}, "max_turns"),
        ({"simulator": ScriptedResponder(), "max_turns": 0}, "max_turns"),
        ({"simulator": ScriptedResponder(), "starts_with": "other"}, "starts_with"),
    ],
)
def test_entry_validation_happens_before_responder_calls(
    kwargs: dict[str, Any], match: str
) -> None:
    agent = ScriptedResponder(ConversationResponse(content="unused"))
    with pytest.raises(KensaCaseError, match=match):
        kensa_case(id="invalid", input="x").run(agent, **kwargs)
    assert agent.calls == 0


@pytest.mark.parametrize("instructions", ["", " ", "\n\t"])
def test_llm_simulator_validates_constructor(instructions: str) -> None:
    with pytest.raises(LLMConfigurationError, match="instructions"):
        LLMSimulator(instructions)


@pytest.mark.parametrize("temperature", [math.nan, math.inf, -math.inf])
def test_llm_simulator_rejects_non_finite_temperature(temperature: float) -> None:
    with pytest.raises(LLMConfigurationError, match="temperature"):
        LLMSimulator("customer", temperature=temperature)


@pytest.mark.parametrize("temperature", [True, "hot"])
def test_llm_simulator_rejects_non_numeric_temperature(temperature: Any) -> None:
    with pytest.raises(LLMConfigurationError, match="temperature"):
        LLMSimulator("customer", temperature=temperature)


@pytest.mark.asyncio
async def test_llm_simulator_seeds_empty_history_with_user_kickoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_acomplete(messages: list[dict[str, Any]], **kwargs: Any) -> LLMResult:
        calls.append({"messages": messages, **kwargs})
        return LLMResult(
            content='{"content":"hello","termination_reason":null}',
            parsed={"content": "hello", "termination_reason": None},
        )

    monkeypatch.setattr(conversation, "acomplete", fake_acomplete)

    response = await LLMSimulator("Act as a customer").respond(())

    assert response == ConversationResponse(content="hello")
    assert calls[0]["messages"][-1] == {
        "role": "user",
        "content": "Begin the scenario with the first simulated user response.",
    }


@pytest.mark.asyncio
async def test_llm_simulator_uses_native_async_completion_and_inverts_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_acomplete(messages: list[dict[str, Any]], **kwargs: Any) -> LLMResult:
        calls.append({"messages": messages, **kwargs})
        return LLMResult(
            content='{"content":"next","termination_reason":null}',
            provider="openai",
            model="gpt-5.4-mini",
            parsed={"content": "next", "termination_reason": None},
        )

    monkeypatch.setattr(conversation, "acomplete", fake_acomplete)
    simulator = LLMSimulator("Act as a customer")

    response = await simulator.respond(
        (
            {"role": "user", "content": ""},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "customer said"},
            {"role": "assistant", "content": "agent said"},
        )
    )

    assert response == ConversationResponse(content="next", termination_reason=None)
    assert calls[0]["messages"][-4:] == [
        {"role": "assistant", "content": ""},
        {"role": "user", "content": ""},
        {"role": "assistant", "content": "customer said"},
        {"role": "user", "content": "agent said"},
    ]
    assert calls[0]["response_format"].__name__ == "_LLMSimulatorResponse"


@pytest.mark.asyncio
async def test_llm_simulator_missing_structured_result_is_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_completion(**kwargs: Any) -> Any:
        del kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="malformed", parsed=None))],
            usage=None,
        )

    monkeypatch.setattr("kensa.llm._acompletion", fake_completion)
    agent = ScriptedResponder(ConversationResponse(content="unused"))
    with pytest.raises(ConversationError) as raised:
        await kensa_case(
            id="malformed",
            messages=[{"role": "user", "content": "accepted initial"}],
        ).run(
            agent,
            simulator=LLMSimulator("customer"),
            max_turns=1,
        )
    assert raised.value.kind == "contract"
    assert raised.value.source == "simulator"
    assert raised.value.messages == ({"role": "user", "content": "accepted initial"},)
    assert agent.calls == 0

    provider_failure = LLMProviderError("transport failed")

    async def failed_completion(**kwargs: Any) -> Any:
        del kwargs
        raise provider_failure

    monkeypatch.setattr("kensa.llm._acompletion", failed_completion)
    with pytest.raises(ConversationError) as execution:
        await kensa_case(id="provider_failure", input="x").run(
            agent,
            simulator=LLMSimulator("customer"),
            max_turns=1,
        )
    assert execution.value.kind == "execution"
    assert execution.value.__cause__ is provider_failure


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(choices=[], usage=None),
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None, parsed=None))],
            usage=None,
        ),
    ],
)
@pytest.mark.asyncio
async def test_llm_simulator_malformed_response_shape_is_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
    response: Any,
) -> None:
    async def fake_completion(**kwargs: Any) -> Any:
        del kwargs
        return response

    monkeypatch.setattr("kensa.llm._acompletion", fake_completion)

    with pytest.raises(ConversationError) as raised:
        await kensa_case(id="malformed_shape", input="x").run(
            ScriptedResponder(ConversationResponse(content="unused")),
            simulator=LLMSimulator("customer"),
            max_turns=1,
        )

    assert raised.value.kind == "contract"
    assert raised.value.source == "simulator"


@pytest.mark.asyncio
async def test_llm_simulator_invalid_parsed_result_is_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_acomplete(messages: list[dict[str, Any]], **kwargs: Any) -> LLMResult:
        del messages, kwargs
        return LLMResult(content="malformed", parsed={"content": 1})

    monkeypatch.setattr(conversation, "acomplete", fake_acomplete)

    with pytest.raises(ConversationError) as raised:
        await kensa_case(id="invalid_parsed", input="x").run(
            ScriptedResponder(ConversationResponse(content="unused")),
            simulator=LLMSimulator("customer"),
            max_turns=1,
        )

    assert raised.value.kind == "contract"
    assert raised.value.source == "simulator"


@pytest.mark.asyncio
async def test_llm_simulator_schema_validation_error_is_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_acomplete(messages: list[dict[str, Any]], **kwargs: Any) -> LLMResult:
        del messages, kwargs
        Value.model_validate({"items": ["invalid"]})
        raise AssertionError("unreachable")

    monkeypatch.setattr(conversation, "acomplete", fake_acomplete)

    with pytest.raises(ConversationError) as raised:
        await kensa_case(id="schema_validation_error", input="x").run(
            ScriptedResponder(ConversationResponse(content="unused")),
            simulator=LLMSimulator("customer"),
            max_turns=1,
        )

    assert raised.value.kind == "contract"
    assert raised.value.source == "simulator"


@pytest.mark.asyncio
async def test_invalid_simulator_responses_are_contract_failures() -> None:
    invalid = [
        object(),
        ConversationResponse(output=None),
        ConversationResponse(),
        ConversationResponse.model_construct(content=" ", output=None, termination_reason=None),
    ]
    for response in invalid:
        agent = ScriptedResponder(ConversationResponse(content="unused"))
        simulator = ScriptedResponder(response)
        with pytest.raises(ConversationError) as raised:
            await kensa_case(id="bad_sim", input="x").run(
                agent,
                simulator=simulator,
                max_turns=1,
            )
        assert raised.value.kind == "contract"
        assert raised.value.source == "simulator"
        assert raised.value.messages == ()
        assert agent.calls == 0


@pytest.mark.asyncio
async def test_responder_failures_preserve_state_without_retry() -> None:
    original = RuntimeError("boom")
    agent = ScriptedResponder(original)
    simulator = ScriptedResponder(ConversationResponse(content="hello"))

    with pytest.raises(ConversationError) as raised:
        await kensa_case(id="failure", input="x").run(
            agent,
            simulator=simulator,
            max_turns=1,
        )

    assert raised.value.kind == "execution"
    assert raised.value.source == "agent"
    assert raised.value.__cause__ is original
    assert raised.value.messages == ({"role": "user", "content": "hello"},)
    assert raised.value.output is None
    assert agent.calls == 1
    assert simulator.calls == 1

    interruption = KeyboardInterrupt()
    interrupted_agent = ScriptedResponder(interruption)
    with pytest.raises(KeyboardInterrupt) as propagated:
        await kensa_case(id="interrupt", input="x").run(
            interrupted_agent,
            simulator=ScriptedResponder(ConversationResponse(content="hello")),
            max_turns=1,
        )
    assert propagated.value is interruption
    assert interrupted_agent.calls == 1

    class AsyncFailure:
        async def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            raise RuntimeError("async boom")

    with pytest.raises(ConversationError) as async_raised:
        await kensa_case(id="async_failure", input="x").run(AsyncFailure())
    assert async_raised.value.kind == "execution"
    assert isinstance(async_raised.value.__cause__, RuntimeError)

    class AsyncCancellation:
        async def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await kensa_case(id="cancelled", input="x").run(AsyncCancellation())

    simulator_failure = RuntimeError("simulator boom")
    failed_simulator = ScriptedResponder(simulator_failure)
    untouched_agent = ScriptedResponder(ConversationResponse(content="unused"))
    with pytest.raises(ConversationError) as simulator_raised:
        await kensa_case(id="simulator_failure", input="x").run(
            untouched_agent,
            simulator=failed_simulator,
            max_turns=1,
        )
    assert simulator_raised.value.kind == "execution"
    assert simulator_raised.value.source == "simulator"
    assert simulator_raised.value.__cause__ is simulator_failure
    assert untouched_agent.calls == 0


class _ProcessInterruption(BaseException):
    pass


@pytest.mark.parametrize("interruption_type", [SystemExit, GeneratorExit, _ProcessInterruption])
def test_process_interruptions_propagate_unchanged(
    interruption_type: type[BaseException],
) -> None:
    interruption = interruption_type()
    agent = ScriptedResponder(interruption)

    with pytest.raises(interruption_type) as propagated:
        kensa_case(id="process_interruption", input="x").run(agent)

    assert propagated.value is interruption
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_simulator_cancellation_propagates_without_calling_agent() -> None:
    class CancelledSimulator:
        calls = 0

        async def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            self.calls += 1
            raise asyncio.CancelledError

    simulator = CancelledSimulator()
    agent = ScriptedResponder(ConversationResponse(content="unused"))

    with pytest.raises(asyncio.CancelledError):
        await kensa_case(id="simulator_cancelled", input="x").run(
            agent,
            simulator=simulator,
            max_turns=1,
        )

    assert simulator.calls == 1
    assert agent.calls == 0


def test_sync_async_and_dynamic_awaitables_share_semantics() -> None:
    sync = kensa_case(id="sync", input="x").run(
        ScriptedResponder(ConversationResponse(content="ok"))
    )
    assert isinstance(sync, CaseResult)

    class AsyncAgent:
        async def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            assert messages == ()
            return ConversationResponse(content="ok")

    async_result = kensa_case(id="async", input="x").run(AsyncAgent())
    assert inspect.isawaitable(async_result)
    async_value = asyncio.run(cast(Any, async_result))
    assert async_value == sync

    class DynamicAgent:
        def respond(self, messages: tuple[KensaMessage, ...]) -> Any:
            async def result() -> ConversationResponse:
                return ConversationResponse(content="ok")

            return result()

    dynamic = kensa_case(id="dynamic", input="x").run(DynamicAgent())
    assert inspect.isawaitable(dynamic)
    dynamic_value = asyncio.run(cast(Any, dynamic))
    assert dynamic_value == sync

    simulated = kensa_case(id="simulated", input="x").run(
        ScriptedResponder(ConversationResponse(content="done", termination_reason="done")),
        simulator=ScriptedResponder(ConversationResponse(content="hello")),
        max_turns=1,
    )
    assert inspect.isawaitable(simulated)
    simulated_value = asyncio.run(cast(Any, simulated))

    for result in (sync, async_value, dynamic_value, simulated_value):
        assert result.trace.spans == []
        assert not result.trace.incomplete


def test_engine_backed_direct_conversation_preserves_public_result_and_snapshots() -> None:
    snapshots: list[Any] = []
    with EngineClient() as engine:
        runtime = KensaTrialRuntime(
            trial=KensaTrial(1, 1),
            nodeid="test_engine_backed_direct_conversation",
            group_id="group",
            case_id="case",
            no_judge=False,
            snapshot_callback=lambda state: snapshots.append(deepcopy(state.output)),
            engine=engine,
        )
        token = set_current_runtime(runtime)
        try:
            output = Value(items=[1])
            result = kensa_case(
                id="engine-direct",
                messages=[{"role": "user", "content": "hello"}],
            ).run(ScriptedResponder(ConversationResponse(content="done", output=output)))
            assert runtime.finalize_engine({"kind": "passed"}) == ("pass", None)
        finally:
            reset_current_runtime(token)

    assert isinstance(result, CaseResult)
    assert isinstance(result.output, Value)
    assert result == CaseResult(
        messages=(
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "done"},
        ),
        output=Value(items=[1]),
        termination=Termination(source="engine", reason="direct"),
    )
    assert snapshots[0] == {
        "messages": [{"role": "user", "content": "hello"}],
        "output": None,
        "termination": None,
    }
    assert snapshots[1] == {
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "done"},
        ],
        "output": {"items": [1]},
        "termination": None,
    }
    assert snapshots[-1]["termination"] == {"source": "engine", "reason": "direct"}


@pytest.mark.asyncio
async def test_engine_actions_authoritatively_drive_python_responders() -> None:
    initial_messages = (
        {"role": "system", "content": "private"},
        {"role": "user", "content": "initial"},
    )
    first = EngineConversationAction(
        source="agent",
        messages=initial_messages,
        response_index=7,
        agent_responses=3,
        accepted_messages=initial_messages,
        accepted_output=None,
        accepted_output_recorded=False,
    )
    accepted_after_agent = (
        *initial_messages,
        {"role": "assistant", "content": "agent answer"},
    )
    second = EngineConversationAction(
        source="simulator",
        messages=({"role": "assistant", "content": "engine projection"},),
        response_index=11,
        agent_responses=4,
        accepted_messages=accepted_after_agent,
        accepted_output={"items": [1]},
        accepted_output_recorded=True,
    )
    terminal_messages = (
        *accepted_after_agent,
        {"role": "user", "content": "follow-up"},
    )
    terminal = EngineConversationResult(
        messages=terminal_messages,
        output={"items": [1]},
        output_recorded=True,
        termination_source="simulator",
        termination_reason="finished",
    )
    engine = ScriptedConversationEngine(first, second, terminal)
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="scripted-actions",
        group_id="group",
        case_id="case",
        no_judge=False,
        engine=cast(Any, engine),
    )
    agent = ScriptedResponder(ConversationResponse(content="agent answer", output=Value(items=[1])))
    simulator = ScriptedResponder(
        ConversationResponse(content="follow-up", termination_reason="finished")
    )
    token = set_current_runtime(runtime)
    try:
        result = await kensa_case(id="scripted", input="x").run(
            agent,
            simulator=simulator,
            max_turns=1,
            starts_with="simulator",
        )
    finally:
        reset_current_runtime(token)

    assert agent.histories == [initial_messages]
    assert simulator.histories == [({"role": "assistant", "content": "engine projection"},)]
    assert engine.starts[0][1]["starts_with"] == "simulator"
    assert [item[1]["source"] for item in engine.observations] == ["agent", "simulator"]
    assert isinstance(result.output, Value)
    assert result.messages == terminal_messages
    response_spans = [
        span for span in runtime.trace.spans if span.name == "kensa.conversation.respond"
    ]
    assert [span.attributes["kensa.conversation.response_index"] for span in response_spans] == [
        7,
        11,
    ]
    assert [span.attributes["kensa.conversation.agent_responses"] for span in response_spans] == [
        3,
        4,
    ]


def test_engine_contract_failure_preserves_core_accepted_state() -> None:
    first = EngineConversationAction(
        source="agent",
        messages=({"role": "user", "content": "accepted"},),
        response_index=1,
        agent_responses=0,
        accepted_messages=({"role": "user", "content": "accepted"},),
        accepted_output={"kept": True},
        accepted_output_recorded=True,
    )
    engine = ScriptedConversationEngine(
        first,
        KensaEngineError("rejected", code="invalid_message"),
    )
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="contract-failure",
        group_id="group",
        case_id="case",
        no_judge=False,
        engine=cast(Any, engine),
    )
    token = set_current_runtime(runtime)
    try:
        with pytest.raises(ConversationError) as raised:
            kensa_case(id="contract", input="x").run(
                ScriptedResponder(ConversationResponse(content="rejected"))
            )
    finally:
        reset_current_runtime(token)

    assert raised.value.kind == "contract"
    assert raised.value.messages == ({"role": "user", "content": "accepted"},)
    assert raised.value.output == {"kept": True}
    assert runtime.output == {
        "messages": [{"role": "user", "content": "accepted"}],
        "output": {"kept": True},
        "termination": None,
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_turns": 1}, "require a simulator"),
        (
            {
                "simulator": ScriptedResponder(ConversationResponse(content="x")),
                "max_turns": 0,
            },
            "positive",
        ),
        (
            {
                "simulator": ScriptedResponder(ConversationResponse(content="x")),
                "starts_with": cast(Any, "other"),
            },
            "starts_with",
        ),
    ],
)
def test_engine_backed_conversation_validates_native_configuration(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    first = EngineConversationAction(
        source="agent",
        messages=(),
        response_index=1,
        agent_responses=0,
        accepted_messages=(),
        accepted_output=None,
        accepted_output_recorded=False,
    )
    engine = ScriptedConversationEngine(first)
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid=f"invalid-config-{message}",
        group_id="group",
        case_id="case",
        no_judge=False,
        engine=cast(Any, engine),
    )
    token = set_current_runtime(runtime)
    try:
        with pytest.raises(KensaCaseError, match=message):
            kensa_case(id="invalid-config", input="x").run(
                ScriptedResponder(ConversationResponse(content="unused")),
                **kwargs,
            )
    finally:
        reset_current_runtime(token)
    assert engine.starts == []


@pytest.mark.parametrize(
    "response",
    [
        object(),
        ConversationResponse.model_construct(
            content=" ", output=None, termination_reason=None, _fields_set={"content"}
        ),
        ConversationResponse.model_construct(
            content=None,
            output=None,
            termination_reason=" ",
            _fields_set={"termination_reason"},
        ),
        ConversationResponse(output=object()),
    ],
)
def test_engine_backed_invalid_native_responses_are_contract_failures(
    response: object,
) -> None:
    first = EngineConversationAction(
        source="agent",
        messages=(),
        response_index=1,
        agent_responses=0,
        accepted_messages=(),
        accepted_output=None,
        accepted_output_recorded=False,
    )
    engine = ScriptedConversationEngine(first)
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="invalid-native-response",
        group_id="group",
        case_id="case",
        no_judge=False,
        engine=cast(Any, engine),
    )
    token = set_current_runtime(runtime)
    try:
        with pytest.raises(ConversationError) as raised:
            kensa_case(id="invalid-native", input="x").run(ScriptedResponder(response))
    finally:
        reset_current_runtime(token)
    assert raised.value.kind == "contract"
    assert engine.observations == []


@pytest.mark.asyncio
async def test_engine_backed_async_responder_failure_preserves_state() -> None:
    first = EngineConversationAction(
        source="agent",
        messages=({"role": "user", "content": "accepted"},),
        response_index=1,
        agent_responses=0,
        accepted_messages=({"role": "user", "content": "accepted"},),
        accepted_output=None,
        accepted_output_recorded=False,
    )
    engine = ScriptedConversationEngine(first)

    class FailedAgent:
        async def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            del messages
            raise RuntimeError("async failure")

    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="async-engine-failure",
        group_id="group",
        case_id="case",
        no_judge=False,
        engine=cast(Any, engine),
    )
    token = set_current_runtime(runtime)
    try:
        with pytest.raises(ConversationError) as raised:
            await kensa_case(id="async-engine", input="x").run(FailedAgent())
    finally:
        reset_current_runtime(token)
    assert raised.value.kind == "execution"
    assert raised.value.messages == ({"role": "user", "content": "accepted"},)


@pytest.mark.asyncio
async def test_engine_backed_async_direct_responder_completes() -> None:
    first = EngineConversationAction(
        source="agent",
        messages=(),
        response_index=1,
        agent_responses=0,
        accepted_messages=(),
        accepted_output=None,
        accepted_output_recorded=False,
    )
    terminal = EngineConversationResult(
        messages=({"role": "assistant", "content": "done"},),
        output="done",
        output_recorded=True,
        termination_source="engine",
        termination_reason="direct",
    )
    engine = ScriptedConversationEngine(first, terminal)

    class AsyncAgent:
        async def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            assert messages == ()
            return ConversationResponse(content="done")

    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="async-engine-success",
        group_id="group",
        case_id="case",
        no_judge=False,
        engine=cast(Any, engine),
    )
    token = set_current_runtime(runtime)
    try:
        result = await kensa_case(id="async-engine-success", input="x").run(AsyncAgent())
    finally:
        reset_current_runtime(token)
    assert result.output == "done"


def test_engine_backed_sync_responder_failure_preserves_state() -> None:
    first = EngineConversationAction(
        source="agent",
        messages=(),
        response_index=1,
        agent_responses=0,
        accepted_messages=(),
        accepted_output=None,
        accepted_output_recorded=False,
    )
    engine = ScriptedConversationEngine(first)
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="sync-engine-failure",
        group_id="group",
        case_id="case",
        no_judge=False,
        engine=cast(Any, engine),
    )
    token = set_current_runtime(runtime)
    try:
        with pytest.raises(ConversationError) as raised:
            kensa_case(id="sync-engine-failure", input="x").run(
                ScriptedResponder(RuntimeError("sync failure"))
            )
    finally:
        reset_current_runtime(token)
    assert raised.value.kind == "execution"


def test_engine_transport_failure_and_invalid_direct_progression_propagate() -> None:
    first = EngineConversationAction(
        source="agent",
        messages=(),
        response_index=1,
        agent_responses=0,
        accepted_messages=(),
        accepted_output=None,
        accepted_output_recorded=False,
    )
    for step, expected in [
        (KensaEngineError("offline", code="transport"), "offline"),
        (first, "kept a direct conversation active"),
    ]:
        engine = ScriptedConversationEngine(first, step)
        runtime = KensaTrialRuntime(
            trial=KensaTrial(1, 1),
            nodeid=f"engine-propagation-{expected}",
            group_id="group",
            case_id="case",
            no_judge=False,
            engine=cast(Any, engine),
        )
        token = set_current_runtime(runtime)
        try:
            with pytest.raises(KensaEngineError, match=expected):
                kensa_case(id="engine-propagation", input="x").run(
                    ScriptedResponder(ConversationResponse(content="done"))
                )
        finally:
            reset_current_runtime(token)


def test_engine_cannot_request_an_unavailable_responder() -> None:
    first = EngineConversationAction(
        source="simulator",
        messages=(),
        response_index=1,
        agent_responses=0,
        accepted_messages=(),
        accepted_output=None,
        accepted_output_recorded=False,
    )
    engine = ScriptedConversationEngine(first)
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="unavailable-responder",
        group_id="group",
        case_id="case",
        no_judge=False,
        engine=cast(Any, engine),
    )
    token = set_current_runtime(runtime)
    try:
        with pytest.raises(KensaEngineError, match="unavailable simulator"):
            kensa_case(id="unavailable-responder", input="x").run(
                ScriptedResponder(ConversationResponse(content="unused"))
            )
    finally:
        reset_current_runtime(token)


def test_engine_result_uses_authoritative_output_when_typed_candidate_differs() -> None:
    first = EngineConversationAction(
        source="agent",
        messages=(),
        response_index=1,
        agent_responses=0,
        accepted_messages=(),
        accepted_output=None,
        accepted_output_recorded=False,
    )
    terminal = EngineConversationResult(
        messages=(),
        output={"authoritative": True},
        output_recorded=True,
        termination_source="engine",
        termination_reason="direct",
    )
    engine = ScriptedConversationEngine(first, terminal)
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="authoritative-output",
        group_id="group",
        case_id="case",
        no_judge=False,
        engine=cast(Any, engine),
    )
    token = set_current_runtime(runtime)
    try:
        result = kensa_case(id="authoritative-output", input="x").run(
            ScriptedResponder(ConversationResponse(output=[1]))
        )
    finally:
        reset_current_runtime(token)
    assert result.output == {"authoritative": True}


class _EngineProcessInterruption(BaseException):
    pass


@pytest.mark.parametrize(
    "interruption_type",
    [KeyboardInterrupt, SystemExit, GeneratorExit, _EngineProcessInterruption],
)
def test_engine_backed_process_interruptions_propagate_unchanged(
    interruption_type: type[BaseException],
) -> None:
    interruption = interruption_type()
    engine = ScriptedConversationEngine(engine_action("agent"))
    runtime = engine_runtime(engine, f"engine-interruption-{interruption_type.__name__}")
    token = set_current_runtime(runtime)
    try:
        with pytest.raises(interruption_type) as propagated:
            kensa_case(id="engine-interruption", input="x").run(ScriptedResponder(interruption))
    finally:
        reset_current_runtime(token)

    assert propagated.value is interruption
    assert engine.observations == []


@pytest.mark.asyncio
async def test_engine_backed_cancellation_propagates_unchanged() -> None:
    class CancelledAgent:
        async def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            del messages
            raise asyncio.CancelledError

    engine = ScriptedConversationEngine(engine_action("agent"))
    runtime = engine_runtime(engine, "engine-cancelled")
    token = set_current_runtime(runtime)
    try:
        with pytest.raises(asyncio.CancelledError):
            await kensa_case(id="engine-cancelled", input="x").run(CancelledAgent())
    finally:
        reset_current_runtime(token)

    assert engine.observations == []


@pytest.mark.parametrize("failed_source", ["agent", "simulator"])
@pytest.mark.asyncio
async def test_engine_backed_later_turn_failures_keep_last_accepted_state(
    failed_source: Literal["agent", "simulator"],
) -> None:
    first_source: Literal["agent", "simulator"] = (
        "simulator" if failed_source == "agent" else "agent"
    )
    accepted_message: dict[str, Any] = {
        "role": "user" if first_source == "simulator" else "assistant",
        "content": "accepted",
    }
    first = engine_action(first_source)
    second = engine_action(
        failed_source,
        messages=(accepted_message,),
        accepted_messages=(accepted_message,),
        accepted_output="accepted" if first_source == "agent" else None,
        accepted_output_recorded=first_source == "agent",
        response_index=2,
        agent_responses=1 if first_source == "agent" else 0,
    )
    engine = ScriptedConversationEngine(first, second)
    failure = RuntimeError(f"{failed_source} failed")
    agent = ScriptedResponder(
        failure if failed_source == "agent" else ConversationResponse(content="accepted")
    )
    simulator = ScriptedResponder(
        failure if failed_source == "simulator" else ConversationResponse(content="accepted")
    )
    runtime = engine_runtime(engine, f"later-{failed_source}-failure")
    token = set_current_runtime(runtime)
    try:
        with pytest.raises(ConversationError) as raised:
            await kensa_case(id="later-failure", input="x").run(
                agent,
                simulator=simulator,
                max_turns=2,
                starts_with=first_source,
            )
    finally:
        reset_current_runtime(token)

    assert raised.value.source == failed_source
    assert raised.value.kind == "execution"
    assert raised.value.__cause__ is failure
    assert raised.value.messages == (accepted_message,)
    assert raised.value.output == ("accepted" if first_source == "agent" else None)
    assert len(engine.observations) == 1


@pytest.mark.asyncio
async def test_engine_backed_dynamic_awaitable_can_move_to_new_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    terminal = EngineConversationResult(
        messages=({"role": "assistant", "content": "done"},),
        output="done",
        output_recorded=True,
        termination_source="engine",
        termination_reason="direct",
    )
    engine = ScriptedConversationEngine(engine_action("agent"), terminal)

    class DynamicAgent:
        def respond(self, messages: tuple[KensaMessage, ...]) -> Any:
            assert messages == ()

            async def result() -> ConversationResponse:
                await asyncio.sleep(0)
                return ConversationResponse(content="done")

            return result()

    runtime = engine_runtime(engine, "engine-dynamic-new-task")
    token = set_current_runtime(runtime)
    try:
        pending = kensa_case(id="engine-dynamic", input="x").run(DynamicAgent())
        assert inspect.isawaitable(pending)
        await asyncio.sleep(0)
        result = await asyncio.create_task(cast(Any, pending))
    finally:
        reset_current_runtime(token)

    assert result.output == "done"
    assert result.trace is runtime.trace
    assert not any("Failed to detach context" in record.getMessage() for record in caplog.records)
    trial_span = next(span for span in runtime.trace.spans if span.name == "kensa.pytest.trial")
    response_span = next(
        span for span in runtime.trace.spans if span.name == "kensa.conversation.respond"
    )
    assert response_span.parent_span_id == trial_span.span_id


@pytest.mark.asyncio
async def test_async_case_run_can_move_to_a_new_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class AsyncAgent:
        async def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            await asyncio.sleep(0)
            return ConversationResponse(content="ok")

    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test_async_case_run_can_move_to_a_new_task",
        group_id="group",
        case_id="case",
        no_judge=False,
    )
    token = set_current_runtime(runtime)
    try:
        pending = kensa_case(id="new_task", input="x").run(AsyncAgent())
        await asyncio.sleep(0)
        result = await asyncio.create_task(cast(Any, pending))
    finally:
        reset_current_runtime(token)

    assert result.output == "ok"
    assert result.trace is runtime.trace
    assert result.trace.spans
    assert not any("Failed to detach context" in record.getMessage() for record in caplog.records)
    trial_span = next(span for span in runtime.trace.spans if span.name == "kensa.pytest.trial")
    response_span = next(
        span for span in runtime.trace.spans if span.name == "kensa.conversation.respond"
    )
    assert response_span.parent_span_id == trial_span.span_id


def test_runtime_snapshots_initial_accepted_failure_and_success() -> None:
    snapshots: list[Any] = []
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test_runtime_snapshots",
        group_id="group",
        case_id="case",
        no_judge=False,
        snapshot_callback=lambda state: snapshots.append(deepcopy(state.output)),
    )
    token = set_current_runtime(runtime)
    try:
        value = Value(items=[1])
        result = kensa_case(
            id="snapshot",
            messages=[{"role": "user", "content": "hello"}],
        ).run(ScriptedResponder(ConversationResponse(content="done", output=value)))
    finally:
        reset_current_runtime(token)

    assert isinstance(result, CaseResult)
    assert isinstance(result.output, Value)
    assert snapshots[0] == {
        "messages": [{"role": "user", "content": "hello"}],
        "output": None,
        "termination": None,
    }
    assert snapshots[1] == {
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "done"},
        ],
        "output": {"items": [1]},
        "termination": None,
    }
    assert snapshots[-1] == {
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "done"},
        ],
        "output": {"items": [1]},
        "termination": {"source": "engine", "reason": "direct"},
    }
    cast(Value, result.output).items.append(2)
    assert snapshots[-1]["output"] == {"items": [1]}


@pytest.mark.asyncio
async def test_runtime_failure_snapshot_retains_last_accepted_state() -> None:
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test_runtime_failure_snapshot",
        group_id="group",
        case_id="case",
        no_judge=False,
    )
    token = set_current_runtime(runtime)
    try:
        with pytest.raises(ConversationError):
            await kensa_case(id="failed_snapshot", input="x").run(
                ScriptedResponder(RuntimeError("boom")),
                simulator=ScriptedResponder(ConversationResponse(content="accepted")),
                max_turns=1,
            )
    finally:
        reset_current_runtime(token)

    assert runtime.output == {
        "messages": [{"role": "user", "content": "accepted"}],
        "output": None,
        "termination": None,
    }


def test_runtime_first_response_failure_retains_initial_snapshot() -> None:
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test_runtime_first_response_failure",
        group_id="group",
        case_id="case",
        no_judge=False,
    )
    token = set_current_runtime(runtime)
    try:
        with pytest.raises(ConversationError):
            kensa_case(
                id="failed_initial_snapshot",
                messages=[
                    {"role": "system", "content": "private"},
                    {"role": "user", "content": "initial"},
                ],
            ).run(ScriptedResponder(RuntimeError("boom")))
    finally:
        reset_current_runtime(token)

    assert runtime.output == {
        "messages": [
            {"role": "system", "content": "private"},
            {"role": "user", "content": "initial"},
        ],
        "output": None,
        "termination": None,
    }


@pytest.mark.asyncio
async def test_response_spans_attribute_sources_and_failures() -> None:
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test_response_spans",
        group_id="group",
        case_id="case",
        no_judge=False,
    )
    token = set_current_runtime(runtime)
    try:
        with pytest.raises(ConversationError):
            await kensa_case(id="spans", input="x").run(
                ScriptedResponder(RuntimeError("boom")),
                simulator=ScriptedResponder(ConversationResponse(content="hello")),
                max_turns=1,
            )
    finally:
        reset_current_runtime(token)

    spans = [span for span in runtime.trace.spans if span.name == "kensa.conversation.respond"]
    assert [span.attributes["kensa.conversation.source"] for span in spans] == [
        "simulator",
        "agent",
    ]
    assert [span.attributes["kensa.conversation.response_index"] for span in spans] == [1, 2]
    assert [span.attributes["kensa.conversation.agent_responses"] for span in spans] == [0, 0]
    assert spans[-1].status == "error"


def test_error_state_is_not_aliased() -> None:
    nested = {"items": [1]}
    agent = ScriptedResponder(
        ConversationResponse(output=nested, content="accepted"),
        object(),
    )
    simulator = ScriptedResponder(ConversationResponse(content="continue"))

    async def run() -> ConversationError:
        with pytest.raises(ConversationError) as raised:
            await kensa_case(id="aliases", input="x").run(
                agent,
                simulator=simulator,
                max_turns=2,
                starts_with="agent",
            )
        return raised.value

    error = asyncio.run(run())
    nested["items"].append(2)
    assert error.output == {"items": [1]}
    cast(dict[str, Any], error.output)["items"].append(3)
    assert agent.responses == []


def test_protocols_are_structural_for_type_checkers() -> None:
    agent: ConversationAgent = cast(Any, ScriptedResponder(ConversationResponse()))
    simulator: Simulator = cast(Any, ScriptedResponder(ConversationResponse()))
    assert callable(agent.respond)
    assert callable(simulator.respond)


def test_non_conversation_response_is_agent_contract_failure() -> None:
    with pytest.raises(ConversationError) as raised:
        kensa_case(id="invalid_agent", input="x").run(ScriptedResponder(SimpleNamespace()))
    assert raised.value.kind == "contract"
    assert raised.value.source == "agent"


def test_bypassed_blank_termination_is_agent_contract_failure() -> None:
    response = ConversationResponse.model_construct(
        content=None,
        output=None,
        termination_reason=" ",
        _fields_set={"termination_reason"},
    )
    with pytest.raises(ConversationError, match="termination_reason") as raised:
        kensa_case(id="blank_reason", input="x").run(ScriptedResponder(response))
    assert raised.value.kind == "contract"
