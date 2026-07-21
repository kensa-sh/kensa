"""Minimal internal LLM adapter."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from kensa.errors import KensaTimeoutError
from kensa.models import LLMConfig, LLMModel, LLMProvider

DEFAULT_LLM_MODEL = LLMModel.GPT_5_4_MINI.value

LLMModelInput = LLMModel | str | None
LLMProviderInput = LLMProvider | str | None
ResponseModel = TypeVar("ResponseModel", bound=BaseModel)
StructuredResponseFormat = type[BaseModel]

_MODEL_PROVIDERS: dict[LLMModel, LLMProvider] = {
    LLMModel.GPT_5_4_MINI: LLMProvider.OPENAI,
    LLMModel.GPT_5_5: LLMProvider.OPENAI,
    LLMModel.CLAUDE_SONNET_4_6: LLMProvider.ANTHROPIC,
    LLMModel.CLAUDE_OPUS_4_7: LLMProvider.ANTHROPIC,
}


class LLMConfigurationError(RuntimeError):
    """Raised when an LLM call is requested without enough configuration."""


class LLMProviderError(RuntimeError):
    """Raised when the configured LLM provider cannot be used."""


@dataclass(frozen=True)
class LLMResult:
    content: str
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    parsed: Any = None


def complete(
    messages: list[dict[str, Any]],
    *,
    model: LLMModelInput = None,
    provider: LLMProviderInput = None,
    temperature: float | None = 0.0,
    response_format: StructuredResponseFormat | None = None,
    metadata: dict[str, Any] | None = None,
    timeout_s: float | None = None,
) -> LLMResult:
    """Run a single chat-style LLM completion through Any LLM."""

    response_format, config, kwargs = _completion_args(
        messages,
        model=model,
        provider=provider,
        temperature=temperature,
        response_format=response_format,
        timeout_s=timeout_s,
    )
    response = _completion(**kwargs)
    return _completion_result(response, config, response_format, metadata)


async def acomplete(
    messages: list[dict[str, Any]],
    *,
    model: LLMModelInput = None,
    provider: LLMProviderInput = None,
    temperature: float | None = 0.0,
    response_format: StructuredResponseFormat | None = None,
    metadata: dict[str, Any] | None = None,
    timeout_s: float | None = None,
) -> LLMResult:
    """Run one chat completion through Any LLM's native async API."""

    response_format, config, kwargs = _completion_args(
        messages,
        model=model,
        provider=provider,
        temperature=temperature,
        response_format=response_format,
        timeout_s=timeout_s,
    )
    response = await _acompletion(**kwargs)
    return _completion_result(response, config, response_format, metadata)


def _completion_args(
    messages: list[dict[str, Any]],
    *,
    model: LLMModelInput,
    provider: LLMProviderInput,
    temperature: float | None,
    response_format: StructuredResponseFormat | None,
    timeout_s: float | None,
) -> tuple[StructuredResponseFormat | None, LLMConfig, dict[str, Any]]:
    validated_format = _validated_response_format(response_format)
    config = resolve_llm_config(model=model, provider=provider)
    kwargs: dict[str, Any] = {
        "model": config.model.value,
        "messages": messages,
        "provider": config.provider.value,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if validated_format is not None:
        kwargs["response_format"] = validated_format
    if timeout_s is not None:
        kwargs["client_args"] = {"timeout": timeout_s, "max_retries": 0}
    return validated_format, config, kwargs


def _completion_result(
    response: Any,
    config: LLMConfig,
    response_format: StructuredResponseFormat | None,
    metadata: dict[str, Any] | None,
) -> LLMResult:
    message = _chat_message(response)
    input_tokens, output_tokens, total_tokens = _extract_usage(response)
    return LLMResult(
        content=_message_content(message),
        provider=config.provider.value,
        model=config.model.value,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        metadata=metadata or {},
        parsed=_message_parsed(message, response_format),
    )


def validate_structured_result(
    result: LLMResult,
    response_format: type[ResponseModel],
) -> ResponseModel:
    """Validate parsed structured output with Kensa's response schema."""

    if result.parsed is None:
        raise LLMProviderError("LLM response did not include parsed structured output.")
    return response_format.model_validate(result.parsed)


def _validated_response_format(
    response_format: Any,
) -> StructuredResponseFormat | None:
    if response_format is None:
        return None
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        return response_format
    raise LLMConfigurationError("response_format must be a Pydantic BaseModel subclass.")


def resolve_llm_config(
    *,
    model: LLMModelInput = None,
    provider: LLMProviderInput = None,
) -> LLMConfig:
    """Resolve explicit arguments, environment, and defaults into an LLM config."""

    resolved_model = (
        _model_value(model)
        or _model_value(os.environ.get("KENSA_LLM_MODEL"))
        or LLMModel.GPT_5_4_MINI
    )
    raw_provider = provider if provider is not None else os.environ.get("KENSA_LLM_PROVIDER")
    resolved_provider = _provider_value(raw_provider) or _default_provider_for_model(resolved_model)
    return LLMConfig(provider=resolved_provider, model=resolved_model)


def _completion(**kwargs: Any) -> Any:
    try:
        from any_llm import completion
    except ImportError as exc:
        raise LLMProviderError(
            "Any LLM is not installed. Install Kensa with its runtime dependencies."
        ) from exc
    try:
        return completion(**kwargs)
    except Exception as exc:
        if _is_timeout_error(exc):
            raise KensaTimeoutError(str(exc) or "LLM completion timed out") from exc
        raise


async def _acompletion(**kwargs: Any) -> Any:
    try:
        from any_llm import acompletion
    except ImportError as exc:
        raise LLMProviderError(
            "Any LLM is not installed. Install Kensa with its runtime dependencies."
        ) from exc
    try:
        return await acompletion(**kwargs)
    except Exception as exc:
        if _is_timeout_error(exc):
            raise KensaTimeoutError(str(exc) or "LLM completion timed out") from exc
        raise


def _is_timeout_error(exc: Exception) -> bool:
    current: Exception | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        exception_type = type(current)
        provider = exception_type.__module__.partition(".")[0]
        if isinstance(current, TimeoutError) or (
            exception_type.__name__ == "APITimeoutError" and provider in {"anthropic", "openai"}
        ):
            return True
        original = getattr(current, "original_exception", None)
        current = original if isinstance(original, Exception) else None
    return False


def _model_value(model: LLMModelInput) -> LLMModel | None:
    if model is None or isinstance(model, LLMModel):
        return model
    try:
        return LLMModel(model)
    except ValueError as exc:
        raise LLMConfigurationError(f"Unsupported LLM model: {model}") from exc


def _provider_value(provider: LLMProviderInput) -> LLMProvider | None:
    if provider is None or isinstance(provider, LLMProvider):
        return provider
    try:
        return LLMProvider(provider)
    except ValueError as exc:
        raise LLMConfigurationError(f"Unsupported LLM provider: {provider}") from exc


def _default_provider_for_model(model: LLMModel) -> LLMProvider:
    return _MODEL_PROVIDERS[model]


def _chat_message(response: Any) -> Any:
    try:
        message = response.choices[0].message
    except (AttributeError, IndexError, TypeError) as exc:
        raise LLMProviderError("LLM response did not include a chat message.") from exc
    if message is None:
        raise LLMProviderError("LLM response did not include a chat message.")
    return message


def _message_content(message: Any) -> str:
    content = getattr(message, "content", None)
    if content is not None:
        return str(content)
    raise LLMProviderError("LLM response message did not include content.")


def _message_parsed(message: Any, response_format: Any) -> Any:
    if response_format is None:
        return None
    parsed = getattr(message, "parsed", None)
    if parsed is not None:
        return parsed
    raise LLMProviderError("LLM response did not include parsed structured output.")


def _extract_usage(response: Any) -> tuple[int | None, int | None, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None, None
    input_tokens = _int_value(_first_usage_attr(usage, "prompt_tokens", "input_tokens"))
    output_tokens = _int_value(_first_usage_attr(usage, "completion_tokens", "output_tokens"))
    total_tokens = _int_value(_first_usage_attr(usage, "total_tokens"))
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = (input_tokens or 0) + (output_tokens or 0)
    return input_tokens, output_tokens, total_tokens


def _first_usage_attr(usage: Any, *names: str) -> Any:
    for name in names:
        value = getattr(usage, name, None)
        if value is not None:
            return value
    return None


def _int_value(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_LLM_MODEL",
    "LLMConfigurationError",
    "LLMModelInput",
    "LLMProviderError",
    "LLMProviderInput",
    "LLMResult",
    "acomplete",
    "complete",
    "resolve_llm_config",
    "validate_structured_result",
]
