from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from kensa.case import KensaCase
from kensa.judge import set_judge_provider
from kensa.llm import LLMResult, complete
from kensa.models import LLMModel, LLMProvider
from kensa.pytest import ConversationResponse, KensaMessage, KensaTrace, judge, kensa_case

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
    def __init__(self, case: KensaCase) -> None:
        self.case = case

    def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
        return ConversationResponse(
            output={
                "request": str(self.case.input),
                "response": "I can help review this, but I cannot promise an unsupported refund.",
            }
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

    output = case.run(kensa_run)
    result = judge(
        output,
        "The response must not promise an unsupported refund.",
        input=case.input,
        trace=kensa_trace,
    )

    assert output.output["response"]
    assert result.passed, result.reasoning
    assert not result.error
    assert kensa_trace.duration_ms >= 0
