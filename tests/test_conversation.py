from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Awaitable
from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

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
