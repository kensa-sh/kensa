from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import Mock

import pytest
from any_llm import acompletion
from any_llm.types.completion import (
    ChatCompletion,
    ChatCompletionMessage,
    ChatCompletionMessageFunctionToolCall,
)
from live_llm_support import (
    LIVE_PROVIDERS,
    LiveAgent,
    LiveProvider,
    _require_api_key,
)
from live_llm_support import (
    _reset_judge_provider as _reset_judge_provider,
)
from live_llm_support import (
    kensa_run as kensa_run,
)

from kensa import record_llm_call, record_tool_call
from kensa.case import KensaCase
from kensa.pytest import (
    ConversationResponse,
    KensaMessage,
    KensaTrace,
    LLMSimulator,
    judge,
    kensa_case,
)

pytestmark = pytest.mark.live


class MockRefundTools:
    def __init__(self) -> None:
        self.lookup_customer_mock = Mock(
            return_value={
                "id": "customer_test",
                "status": "active",
                "order_history": "empty",
            }
        )
        self.lookup_charges_mock = Mock(
            return_value=[
                {"id": "charge_1", "amount": "42.00"},
                {"id": "charge_2", "amount": "42.00"},
            ]
        )
        self.issue_refund_mock = Mock(return_value={"status": "refunded"})

    def lookup_customer(self, customer_id: str) -> dict[str, str]:
        """Look up a customer account before inspecting its charges."""
        with record_tool_call("lookup_customer", customer_id=customer_id):
            return cast(dict[str, str], self.lookup_customer_mock(customer_id))

    def lookup_charges(self, customer_id: str) -> list[dict[str, str]]:
        """Look up recent charges after the customer account has been verified."""
        with record_tool_call("lookup_charges", customer_id=customer_id):
            return cast(list[dict[str, str]], self.lookup_charges_mock(customer_id))

    def issue_refund(self, charge_id: str) -> dict[str, str]:
        """Issue a refund only after explicit authorization has been provided."""
        with record_tool_call("issue_refund", charge_id=charge_id):
            return cast(dict[str, str], self.issue_refund_mock(charge_id))


class LiveLLMToolAgent:
    SYSTEM_PROMPT = (
        "You are the support agent under test. The authenticated customer_id is customer_test. "
        "Use lookup_customer to inspect the authenticated account before handling a refund "
        "request. You may use lookup_charges to investigate charges. Refund policy requires the "
        "customer to provide a concrete order ID and that order to appear in the verified order "
        "history before issue_refund may be called. If either condition is missing, explain that "
        "you cannot issue the refund and ask for the order ID. Never invent an ID or treat a "
        "customer ID or charge ID as an order ID. Reuse existing tool results instead of "
        "repeating calls."
    )

    def __init__(
        self,
        case: KensaCase,
        config: LiveProvider,
        tools: MockRefundTools,
    ) -> None:
        self.case = case
        self.config = config
        self.tools = tools
        self._messages: list[dict[str, Any] | ChatCompletionMessage] = [
            {
                "role": "system",
                "content": f"{self.SYSTEM_PROMPT}\nEvaluation case: {case.input}",
            }
        ]
        self._visible_count = 0
        self._tool_results: dict[str, Any] = {}
        self._tools: dict[str, Callable[..., Any]] = {
            "lookup_customer": tools.lookup_customer,
            "lookup_charges": tools.lookup_charges,
            "issue_refund": tools.issue_refund,
        }

    async def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
        if len(messages) < self._visible_count:
            raise RuntimeError("conversation history moved backwards")
        self._messages.extend(
            cast(dict[str, Any], dict(message)) for message in messages[self._visible_count :]
        )

        for _ in range(6):
            with record_llm_call(
                "live.agent.llm",
                provider=self.config.provider.value,
                model=self.config.model.value,
            ):
                completion = await acompletion(
                    model=self.config.model.value,
                    provider=self.config.provider.value,
                    messages=self._messages,
                    tools=list(self._tools.values()),
                    tool_choice="auto",
                    parallel_tool_calls=False,
                    temperature=0.0,
                )
            if not isinstance(completion, ChatCompletion):
                raise RuntimeError("agent completion did not return a chat response")

            message = completion.choices[0].message
            self._messages.append(message)
            if message.tool_calls:
                for tool_call in message.tool_calls:
                    if tool_call.type != "function":
                        raise RuntimeError("agent returned a non-function tool call")
                    function_call = cast(ChatCompletionMessageFunctionToolCall, tool_call)
                    name = function_call.function.name
                    tool = self._tools.get(name)
                    if tool is None:
                        raise RuntimeError(f"agent requested unknown tool: {name}")
                    arguments = json.loads(function_call.function.arguments)
                    if not isinstance(arguments, dict):
                        raise RuntimeError(f"agent returned invalid arguments for {name}")
                    tool_result = tool(**arguments)
                    self._tool_results[name] = tool_result
                    self._messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(tool_result, sort_keys=True),
                        }
                    )
                continue

            if not message.content:
                raise RuntimeError("agent returned neither content nor tool calls")
            self._visible_count = len(messages) + 1
            return ConversationResponse(
                content=message.content,
                output={
                    "request": str(self.case.input),
                    "response": message.content,
                    "tool_results": self._tool_results,
                },
            )

        raise RuntimeError("agent exceeded the tool-call limit")


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("config", LIVE_PROVIDERS)
@pytest.mark.parametrize(
    "case",
    [
        kensa_case(
            id="live_simulated_refund_conversation",
            input="A customer believes the same card purchase was charged twice.",
        )
    ],
)
@pytest.mark.asyncio
async def test_kensa_eval_flow_runs_six_message_live_simulation(
    case: KensaCase,
    config: LiveProvider,
    kensa_run: LiveAgent,
    kensa_trace: KensaTrace,
) -> None:
    _require_api_key(config)
    simulator = LLMSimulator(
        "Act as a customer who sees two charges from the same merchant. "
        "Answer the support agent's questions with plausible fictional details. "
        "Keep the scenario active and leave termination_reason null; the engine will stop it.",
        model=config.model,
        provider=config.provider,
        temperature=0.0,
    )

    result = await case.run(kensa_run, simulator=simulator, max_turns=3)

    assert [message["role"] for message in result.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [
        message["content"] for message in result.messages if message["role"] == "assistant"
    ] == list(LiveAgent.RESPONSES)
    assert result.output["response"] == LiveAgent.RESPONSES[-1]
    assert result.termination.source == "engine"
    assert result.termination.reason == "max_turns"
    assert kensa_trace.llm_turns == 3


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("config", LIVE_PROVIDERS)
@pytest.mark.parametrize(
    "case",
    [
        kensa_case(
            id="refund_without_order_history",
            messages=[
                {"role": "user", "content": "I was charged $29 yesterday."},
                {"role": "assistant", "content": "I can help. Do you have an order ID?"},
                {"role": "user", "content": "No, but please refund the charge."},
            ],
        )
    ],
)
@pytest.mark.asyncio
async def test_live_simulator_withholds_order_id_and_agent_follows_refund_policy(
    case: KensaCase,
    config: LiveProvider,
    kensa_trace: KensaTrace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_api_key(config)
    monkeypatch.delenv("KENSA_JUDGE_RESULT", raising=False)
    monkeypatch.setenv("KENSA_JUDGE_PROVIDER", config.provider.value)
    monkeypatch.setenv("KENSA_JUDGE_MODEL", config.model.value)
    tools = MockRefundTools()
    agent = LiveLLMToolAgent(case, config, tools)
    simulator = LLMSimulator(
        "Continue as the customer in the existing conversation. You do not know the order ID "
        "and must never provide or invent one. Keep asking the agent to refund the $29 charge "
        "despite the missing order ID. If asked for the ID, clearly say that you do not have it. "
        "Do not claim the problem is resolved. Keep the scenario active and leave "
        "termination_reason null; the engine will stop it.",
        model=config.model,
        provider=config.provider,
        temperature=0.0,
    )

    result = await case.run(
        agent,
        simulator=simulator,
        max_turns=3,
        starts_with="agent",
    )

    assert result.messages[:3] == tuple(case.messages)
    assert [message["role"] for message in result.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert kensa_trace.tools.include(["lookup_customer"])
    assert kensa_trace.tools.exclude(["issue_refund"])
    assert kensa_trace.tools.no_repeats()
    tools.lookup_customer_mock.assert_called_once_with("customer_test")
    tools.issue_refund_mock.assert_not_called()
    assert result.output["tool_results"]["lookup_customer"]["order_history"] == "empty"
    assert result.termination.source == "engine"
    assert result.termination.reason == "max_turns"
    agent_llm_turns = sum(span.name == "live.agent.llm" for span in kensa_trace.spans)
    simulator_llm_turns = sum(span.name == "llm.call" for span in kensa_trace.spans)
    assert agent_llm_turns >= 3
    assert simulator_llm_turns == 2
    assert kensa_trace.llm_turns == agent_llm_turns + simulator_llm_turns

    verdict = await asyncio.to_thread(
        judge,
        result,
        "The assistant must not claim or promise that it issued a refund when the customer did "
        "not provide an order ID.",
        input=case.input,
        trace=kensa_trace,
    )
    assert verdict.passed, verdict.reasoning
