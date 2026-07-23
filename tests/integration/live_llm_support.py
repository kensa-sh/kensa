from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from kensa.case import KensaCase
from kensa.judge import set_judge_provider
from kensa.models import LLMModel, LLMProvider
from kensa.pytest import ConversationResponse, KensaMessage


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


@pytest.fixture(autouse=True)
def _reset_judge_provider() -> None:
    set_judge_provider(None)


@pytest.fixture
def kensa_run(case: KensaCase) -> LiveAgent:
    return LiveAgent(case)


def _require_api_key(config: LiveProvider) -> None:
    if not os.environ.get(config.api_key_env):
        pytest.skip(f"{config.api_key_env} is not set")
