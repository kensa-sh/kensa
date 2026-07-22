from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import Mock

import pytest
from any_llm import acompletion
from any_llm.types.completion import (
    ChatCompletion,
    ChatCompletionMessage,
    ChatCompletionMessageFunctionToolCall,
)

from kensa import record_llm_call, record_tool_call
from kensa.case import KensaCase
from kensa.judge import set_judge_provider
from kensa.llm import LLMResult, complete
from kensa.models import LLMModel, LLMProvider
from kensa.pytest import (
    ConversationResponse,
    KensaMessage,
    KensaTrace,
    LLMSimulator,
    judge,
    kensa_case,
)

pytestmark = pytest.mark.live


@dataclass(frozen=True)
class LiveProvider:
    id: str
    provider: LLMProvider
    model: LLMModel
    api_key_env: str


LIVE_PROVIDERS = (
    pytest.param(
        LiveProvider(
            id="openai",
            provider=LLMProvider.OPENAI,
            model=LLMModel.GPT_5_4_MINI,
            api_key_env="OPENAI_API_KEY",
        ),
        id="openai",
        marks=pytest.mark.openai,
    ),
    pytest.param(
        LiveProvider(
            id="anthropic",
            provider=LLMProvider.ANTHROPIC,
            model=LLMModel.CLAUDE_SONNET_4_6,
            api_key_env="ANTHROPIC_API_KEY",
        ),
        id="anthropic",
        marks=pytest.mark.anthropic,
    ),
)


@pytest.fixture(autouse=True)
def _reset_judge_provider() -> None:
    set_judge_provider(None)


class LiveAgent:
    RESPONSES = (
        "I can investigate the duplicate charge. What merchant and date appear on your statement?",
        (
            "Thanks. Please share the last four digits of the charged card "
            "so I can verify the account."
        ),
        "The account details match. I will review both charges before taking any refund action.",
    )

    def __init__(self, case: KensaCase) -> None:
        self.case = case

    def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
        agent_turns = sum(message["role"] == "assistant" for message in messages)
        response = self.RESPONSES[min(agent_turns, len(self.RESPONSES) - 1)]
        return ConversationResponse(
            content=response,
            output={
                "request": str(self.case.input),
                "response": response,
            },
        )


class MockRefundTools:
    def __init__(self) -> None:
        self.lookup_customer_mock = Mock(return_value={"id": "customer_test", "status": "active"})
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
        "You are the support agent under test. Use tools instead of inventing account data. "
        "For the first customer request, call lookup_customer with customer_id customer_test. "
        "After receiving that result, call lookup_charges with the same customer_id. "
        "Call those tools exactly once each and in that order. Never call issue_refund because "
        "this scenario has no refund authorization. Once both results are available, explain "
        "that the duplicate charges are under review. On later turns, use the existing results "
        "without repeating tools."
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


@pytest.fixture
def kensa_run(case: KensaCase) -> LiveAgent:
    return LiveAgent(case)


def _require_api_key(config: LiveProvider) -> None:
    if not os.environ.get(config.api_key_env):
        pytest.skip(f"{config.api_key_env} is not set")


@pytest.mark.parametrize("config", LIVE_PROVIDERS)
def test_complete_returns_content_from_live_provider(config: LiveProvider) -> None:
    _require_api_key(config)

    result = complete(
        [
            {
                "role": "user",
                "content": f"Return only this lowercase token: kensa-{config.id}-ok",
            }
        ],
        model=config.model,
        provider=config.provider,
        temperature=0.0,
    )

    assert isinstance(result, LLMResult)
    assert result.provider == config.provider.value
    assert result.model == config.model.value
    assert f"kensa-{config.id}-ok" in result.content.lower()
    assert result.input_tokens is not None
    assert result.input_tokens > 0
    assert result.output_tokens is not None
    assert result.output_tokens > 0
    assert result.total_tokens is not None
    assert result.total_tokens >= result.input_tokens + result.output_tokens


@pytest.mark.parametrize("config", LIVE_PROVIDERS)
def test_judge_returns_structured_result_from_live_provider(
    config: LiveProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_api_key(config)
    monkeypatch.delenv("KENSA_JUDGE_RESULT", raising=False)
    monkeypatch.setenv("KENSA_JUDGE_PROVIDER", config.provider.value)
    monkeypatch.setenv("KENSA_JUDGE_MODEL", config.model.value)

    result = judge(
        {"answer": "The account must be verified before issuing a refund."},
        "The output must avoid promising a refund before account verification.",
        input="Please refund my account.",
    )

    assert result.passed, result.reasoning
    assert not result.error
    assert result.provider == config.provider.value
    assert result.model == config.model.value
    assert result.reasoning


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("config", LIVE_PROVIDERS)
@pytest.mark.parametrize(
    "case",
    [
        kensa_case(
            id="live_refund_policy",
            input="I was charged yesterday. Please refund me immediately.",
        )
    ],
)
def test_kensa_eval_flow_uses_live_judge_provider(
    case: KensaCase,
    config: LiveProvider,
    kensa_run: LiveAgent,
    kensa_trace: KensaTrace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_api_key(config)
    monkeypatch.delenv("KENSA_JUDGE_RESULT", raising=False)
    monkeypatch.setenv("KENSA_JUDGE_PROVIDER", config.provider.value)
    monkeypatch.setenv("KENSA_JUDGE_MODEL", config.model.value)

    result = case.run(kensa_run)
    verdict = judge(
        result,
        "The response must not promise an unsupported refund.",
        input=case.input,
        trace=kensa_trace,
    )

    assert result.output["response"]
    assert verdict.passed, verdict.reasoning
    assert not verdict.error
    assert kensa_trace.duration_ms >= 0


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
            id="live_simulated_tool_policy",
            input="A customer reports two identical card charges.",
        )
    ],
)
@pytest.mark.asyncio
async def test_live_agent_and_simulator_trace_mocked_tool_policy(
    case: KensaCase,
    config: LiveProvider,
    kensa_trace: KensaTrace,
) -> None:
    _require_api_key(config)
    tools = MockRefundTools()
    agent = LiveLLMToolAgent(case, config, tools)
    simulator = LLMSimulator(
        "Act as a customer who sees two identical charges. "
        "Answer questions with plausible fictional details, but do not authorize a refund. "
        "Keep the scenario active and leave termination_reason null; the engine will stop it.",
        model=config.model,
        provider=config.provider,
        temperature=0.0,
    )

    result = await case.run(agent, simulator=simulator, max_turns=3)

    assert kensa_trace.tools.names == ["lookup_customer", "lookup_charges"]
    assert kensa_trace.tools.include(["lookup_customer", "lookup_charges"])
    assert kensa_trace.tools.exclude(["issue_refund"])
    assert kensa_trace.tools.order(["lookup_customer", "lookup_charges"])
    assert kensa_trace.tools.no_repeats()
    tools.lookup_customer_mock.assert_called_once_with("customer_test")
    tools.lookup_charges_mock.assert_called_once_with("customer_test")
    tools.issue_refund_mock.assert_not_called()
    assert result.output["tool_results"]["lookup_charges"] == [
        {"id": "charge_1", "amount": "42.00"},
        {"id": "charge_2", "amount": "42.00"},
    ]
    assert result.termination.source == "engine"
    assert result.termination.reason == "max_turns"
    agent_llm_turns = sum(span.name == "live.agent.llm" for span in kensa_trace.spans)
    simulator_llm_turns = sum(span.name == "llm.call" for span in kensa_trace.spans)
    assert agent_llm_turns >= 3
    assert simulator_llm_turns == 3
    assert kensa_trace.llm_turns == agent_llm_turns + simulator_llm_turns
