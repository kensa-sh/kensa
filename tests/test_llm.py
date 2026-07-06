from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from any_llm.constants import LLMProvider as AnyLLMProvider
from pydantic import BaseModel

from kensa.llm import (
    DEFAULT_LLM_MODEL,
    LLMConfigurationError,
    LLMProviderError,
    LLMResult,
    _completion,
    _extract_usage,
    complete,
    resolve_llm_config,
    validate_structured_result,
)
from kensa.models import LLMModel, LLMProvider


def _chat_response(
    *,
    content: str = "ok",
    parsed: Any = None,
    usage: Any = None,
    message: Any = None,
    choices: list[Any] | None = None,
) -> Any:
    if choices is None:
        message = (
            message if message is not None else SimpleNamespace(content=content, parsed=parsed)
        )
        choices = [SimpleNamespace(message=message)]
    return SimpleNamespace(choices=choices, usage=usage)


def test_complete_uses_any_llm_with_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_completion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _chat_response(
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3, total_tokens=13)
        )

    monkeypatch.setenv("KENSA_LLM_MODEL", "gpt-5.5")
    monkeypatch.setenv("KENSA_LLM_PROVIDER", "openai")
    monkeypatch.setattr("kensa.llm._completion", fake_completion)

    result = complete([{"role": "user", "content": "hello"}])

    assert result.content == "ok"
    assert result.provider == "openai"
    assert result.model == "gpt-5.5"
    assert result.input_tokens == 10
    assert result.output_tokens == 3
    assert result.total_tokens == 13
    assert calls == [
        {
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "hello"}],
            "provider": "openai",
            "temperature": 0.0,
        }
    ]


def test_complete_uses_default_model_when_env_model_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_completion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _chat_response()

    monkeypatch.delenv("KENSA_LLM_MODEL", raising=False)
    monkeypatch.delenv("KENSA_LLM_PROVIDER", raising=False)
    monkeypatch.setattr("kensa.llm._completion", fake_completion)

    result = complete([{"role": "user", "content": "hello"}])

    assert result.content == "ok"
    assert result.provider == "openai"
    assert result.model == DEFAULT_LLM_MODEL
    assert calls == [
        {
            "model": DEFAULT_LLM_MODEL,
            "messages": [{"role": "user", "content": "hello"}],
            "provider": "openai",
            "temperature": 0.0,
        }
    ]


def test_complete_passes_response_format_and_handles_provider_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StructuredPayload(BaseModel):
        status: str

    calls: list[dict[str, Any]] = []

    def fake_completion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _chat_response(content='{"status":"ok"}', parsed={"status": "ok"})

    monkeypatch.setattr("kensa.llm._completion", fake_completion)

    result = complete(
        [{"role": "user", "content": "hello"}],
        model=LLMModel.GPT_5_5,
        provider="openai",
        temperature=None,
        response_format=StructuredPayload,
        metadata={"task": "t"},
    )

    assert result.content == '{"status":"ok"}'
    assert result.parsed == {"status": "ok"}
    assert validate_structured_result(result, StructuredPayload) == StructuredPayload(status="ok")
    assert result.metadata == {"task": "t"}
    assert calls == [
        {
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "hello"}],
            "provider": "openai",
            "response_format": StructuredPayload,
        }
    ]


def test_complete_rejects_json_object_response_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_completion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _chat_response()

    monkeypatch.setattr("kensa.llm._completion", fake_completion)
    runtime_response_format: Any = {"type": "json_object"}

    with pytest.raises(LLMConfigurationError, match="Pydantic BaseModel subclass"):
        complete(
            [{"role": "user", "content": "hello"}],
            response_format=runtime_response_format,
        )

    assert calls == []


def test_complete_accepts_enums_and_infers_known_model_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_completion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _chat_response()

    monkeypatch.delenv("KENSA_LLM_MODEL", raising=False)
    monkeypatch.delenv("KENSA_LLM_PROVIDER", raising=False)
    monkeypatch.setattr("kensa.llm._completion", fake_completion)

    result = complete(
        [{"role": "user", "content": "hello"}],
        model=LLMModel.CLAUDE_OPUS_4_7,
        temperature=None,
    )

    assert result.content == "ok"
    assert result.provider == "anthropic"
    assert result.model == "claude-opus-4-7"
    assert calls == [
        {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
            "provider": "anthropic",
        }
    ]

    known_config = resolve_llm_config(model="gpt-5.5")
    assert known_config.model is LLMModel.GPT_5_5
    assert known_config.provider is LLMProvider.OPENAI


def test_completion_forwards_supported_model_slugs_to_any_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_completion(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return kwargs

    monkeypatch.setattr("any_llm.completion", fake_completion)

    for model in (LLMModel.GPT_5_5, LLMModel.CLAUDE_SONNET_4_6, LLMModel.CLAUDE_OPUS_4_7):
        config = resolve_llm_config(model=model)
        assert AnyLLMProvider.from_string(config.provider.value)
        _completion(model=config.model.value, messages=[], provider=config.provider.value)

    assert calls == [
        {"model": "gpt-5.5", "messages": [], "provider": "openai"},
        {"model": "claude-sonnet-4-6", "messages": [], "provider": "anthropic"},
        {"model": "claude-opus-4-7", "messages": [], "provider": "anthropic"},
    ]


def test_resolve_llm_config_rejects_unsupported_model_or_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KENSA_LLM_MODEL", "custom-model")
    with pytest.raises(LLMConfigurationError, match="Unsupported LLM model: custom-model"):
        resolve_llm_config()

    monkeypatch.setenv("KENSA_LLM_MODEL", "gpt-5.5")
    monkeypatch.setenv("KENSA_LLM_PROVIDER", "bogus")
    with pytest.raises(LLMConfigurationError, match="Unsupported LLM provider: bogus"):
        resolve_llm_config()


def test_completion_import_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "any_llm":
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)

    original_import = __import__
    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(LLMProviderError, match="Any LLM"):
        _completion(model="m")

    class FakeAnyLLM:
        @staticmethod
        def completion(**kwargs: Any) -> dict[str, Any]:
            return kwargs

    def fake_import_success(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "any_llm":
            return FakeAnyLLM
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import_success)
    assert _completion(model="m") == {"model": "m"}


def test_complete_rejects_malformed_completion_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ParsedPayload(BaseModel):
        status: str

    monkeypatch.setattr("kensa.llm._completion", lambda **kwargs: {"choices": []})
    with pytest.raises(LLMProviderError, match="chat message"):
        complete([{"role": "user", "content": "hello"}])

    monkeypatch.setattr(
        "kensa.llm._completion",
        lambda **kwargs: _chat_response(choices=[SimpleNamespace(message=None)]),
    )
    with pytest.raises(LLMProviderError, match="chat message"):
        complete([{"role": "user", "content": "hello"}])

    monkeypatch.setattr(
        "kensa.llm._completion",
        lambda **kwargs: _chat_response(message=SimpleNamespace(content=None, parsed=None)),
    )
    with pytest.raises(LLMProviderError, match="content"):
        complete([{"role": "user", "content": "hello"}])

    monkeypatch.setattr(
        "kensa.llm._completion",
        lambda **kwargs: _chat_response(content='{"status":"ok"}', parsed=None),
    )
    with pytest.raises(LLMProviderError, match="parsed structured output"):
        complete(
            [{"role": "user", "content": "hello"}],
            response_format=ParsedPayload,
        )

    with pytest.raises(LLMProviderError, match="parsed structured output"):
        validate_structured_result(LLMResult(content="{}"), ParsedPayload)


def test_extract_usage_response_shapes() -> None:
    openai_usage = SimpleNamespace(prompt_tokens="10", completion_tokens=2)
    assert _extract_usage(SimpleNamespace(usage=openai_usage)) == (
        10,
        2,
        12,
    )
    anthropic_usage = SimpleNamespace(input_tokens=4, output_tokens=5, total_tokens=9)
    assert _extract_usage(SimpleNamespace(usage=anthropic_usage)) == (
        4,
        5,
        9,
    )
    assert _extract_usage(SimpleNamespace(usage=SimpleNamespace(prompt_tokens="bad"))) == (
        None,
        None,
        None,
    )
    assert _extract_usage(SimpleNamespace()) == (None, None, None)

    usage = type(
        "Usage",
        (),
        {"prompt_tokens": 7, "completion_tokens": 8, "total_tokens": 15},
    )()
    response = type("Response", (), {"usage": usage})()

    assert _extract_usage(response) == (7, 8, 15)
