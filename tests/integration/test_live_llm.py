from __future__ import annotations

import pytest
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

from kensa.case import KensaCase
from kensa.llm import LLMResult, complete
from kensa.pytest import KensaTrace, judge, kensa_case

pytestmark = pytest.mark.live


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
