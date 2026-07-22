from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

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
