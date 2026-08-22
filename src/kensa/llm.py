"""Minimal internal LLM adapter."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from time import monotonic, sleep
from typing import Any, TypeVar

from pydantic import BaseModel
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from kensa.errors import KensaTimeoutError
from kensa.models import LLMConfig, LLMModel, LLMProvider

DEFAULT_LLM_MODEL = LLMModel.GPT_5_4_MINI.value
_MAX_LLM_ATTEMPTS = 3
_LLM_RETRY_BASE_DELAY_S = 0.25
_STOP_AFTER_LLM_ATTEMPTS = stop_after_attempt(_MAX_LLM_ATTEMPTS)
_RETRYABLE_ANY_LLM_ERRORS = frozenset(
    {"GatewayTimeoutError", "ProviderError", "RateLimitError", "UpstreamProviderError"}
)
_RETRYABLE_CONNECTION_ERRORS = frozenset(
    {"APIConnectionError", "ConnectError", "ConnectionError", "RemoteProtocolError"}
)
_PROVIDER_MODULES = frozenset({"anthropic", "httpcore", "httpx", "openai"})

LLMModelInput = LLMModel | str | None
LLMProviderInput = LLMProvider | str | None
ResponseModel = TypeVar("ResponseModel", bound=BaseModel)
StructuredResponseFormat = type[BaseModel]

_MODEL_PROVIDERS: dict[LLMModel, LLMProvider] = {
    LLMModel.GPT_5_4_MINI: LLMProvider.OPENAI,
    LLMModel.GPT_5_5: LLMProvider.OPENAI,
    LLMModel.GPT_5_6_LUNA: LLMProvider.OPENAI,
    LLMModel.CLAUDE_SONNET_4_6: LLMProvider.ANTHROPIC,
    LLMModel.CLAUDE_SONNET_5: LLMProvider.ANTHROPIC,
    LLMModel.CLAUDE_OPUS_4_7: LLMProvider.ANTHROPIC,
}


class LLMConfigurationError(RuntimeError):
    """Raised when an LLM call is requested without enough configuration."""


class LLMProviderError(RuntimeError):
    """Raised when the configured LLM provider cannot be used."""


class _LLMStructuredOutputError(LLMProviderError):
    pass


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
    response, attempt_count = _completion_with_retries(kwargs, timeout_s)
    return _completion_result(
        response,
        config,
        response_format,
        _completion_metadata(metadata, attempt_count),
    )


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
    response, attempt_count = await _acompletion_with_retries(kwargs, timeout_s)
    return _completion_result(
        response,
        config,
        response_format,
        _completion_metadata(metadata, attempt_count),
    )


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


def _completion_with_retries(
    kwargs: dict[str, Any],
    timeout_s: float | None,
) -> tuple[Any, int]:
    if timeout_s is None:
        return _completion(**kwargs), 1
    deadline = monotonic() + timeout_s
    last_error: Exception | None = None
    attempt_kwargs = kwargs
    attempt_count = 1

    def prepare_attempt(state: RetryCallState) -> None:
        nonlocal attempt_count, attempt_kwargs
        attempt_count = state.attempt_number
        if attempt_count > 1:
            assert last_error is not None
            attempt_kwargs = _retry_kwargs_or_raise(kwargs, deadline, last_error)

    def run_completion() -> Any:
        nonlocal last_error
        try:
            return _completion(**attempt_kwargs)
        except Exception as exc:
            last_error = exc
            raise

    response = Retrying(
        sleep=sleep,
        retry=retry_if_exception(_is_retryable_error),
        stop=lambda state: _retry_should_stop(state, deadline),
        wait=wait_exponential(multiplier=_LLM_RETRY_BASE_DELAY_S),
        before=prepare_attempt,
        reraise=True,
    )(run_completion)
    return response, attempt_count


async def _acompletion_with_retries(
    kwargs: dict[str, Any],
    timeout_s: float | None,
) -> tuple[Any, int]:
    if timeout_s is None:
        return await _acompletion(**kwargs), 1
    deadline = monotonic() + timeout_s
    last_error: Exception | None = None
    attempt_kwargs = kwargs
    attempt_count = 1

    def prepare_attempt(state: RetryCallState) -> None:
        nonlocal attempt_count, attempt_kwargs
        attempt_count = state.attempt_number
        if attempt_count > 1:
            assert last_error is not None
            attempt_kwargs = _retry_kwargs_or_raise(kwargs, deadline, last_error)

    async def run_completion() -> Any:
        nonlocal last_error
        try:
            return await _acompletion(**attempt_kwargs)
        except Exception as exc:
            last_error = exc
            raise

    response = await AsyncRetrying(
        sleep=asyncio.sleep,
        retry=retry_if_exception(_is_retryable_error),
        stop=lambda state: _retry_should_stop(state, deadline),
        wait=wait_exponential(multiplier=_LLM_RETRY_BASE_DELAY_S),
        before=prepare_attempt,
        reraise=True,
    )(run_completion)
    return response, attempt_count


def _retry_should_stop(state: RetryCallState, deadline: float) -> bool:
    return _STOP_AFTER_LLM_ATTEMPTS(state) or deadline - monotonic() <= state.upcoming_sleep


def _retry_kwargs_or_raise(
    kwargs: dict[str, Any],
    deadline: float,
    last_error: Exception,
) -> dict[str, Any]:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise last_error
    client_args = dict(kwargs["client_args"])
    client_args["timeout"] = remaining
    return {**kwargs, "client_args": client_args}


def _is_retryable_error(exc: BaseException) -> bool:
    if not isinstance(exc, Exception) or isinstance(exc, KensaTimeoutError):
        return False
    chain = _exception_chain(exc)
    statuses = [
        status for current in chain if type(status := getattr(current, "status_code", None)) is int
    ]
    if statuses:
        return any(status == 429 or status >= 500 for status in statuses)
    identities = {(type(current).__module__, type(current).__name__) for current in chain}
    if any(
        module.startswith("any_llm") and name in _RETRYABLE_ANY_LLM_ERRORS
        for module, name in identities
    ):
        return True
    return any(
        module.partition(".")[0] in _PROVIDER_MODULES and name in _RETRYABLE_CONNECTION_ERRORS
        for module, name in identities
    )


def _exception_chain(exc: Exception) -> list[Exception]:
    chain: list[Exception] = []
    current: Exception | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        original = getattr(current, "original_exception", None)
        cause = current.__cause__
        current = (
            original
            if isinstance(original, Exception)
            else cause
            if isinstance(cause, Exception)
            else None
        )
    return chain


def _completion_metadata(
    metadata: dict[str, Any] | None,
    attempt_count: int,
) -> dict[str, Any] | None:
    if attempt_count == 1:
        return metadata
    return {**(metadata or {}), "attempt_count": attempt_count}


def _completion_result(
    response: Any,
    config: LLMConfig,
    response_format: StructuredResponseFormat | None,
    metadata: dict[str, Any] | None,
) -> LLMResult:
    try:
        message = _chat_message(response)
        content = _message_content(message)
        parsed = _message_parsed(message, response_format)
    except _LLMStructuredOutputError:
        raise
    except LLMProviderError as exc:
        if response_format is not None:
            raise _LLMStructuredOutputError(str(exc)) from exc
        raise
    input_tokens, output_tokens, total_tokens = _extract_usage(response)
    return LLMResult(
        content=content,
        provider=config.provider.value,
        model=config.model.value,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        metadata=metadata or {},
        parsed=parsed,
    )


def validate_structured_result(
    result: LLMResult,
    response_format: type[ResponseModel],
) -> ResponseModel:
    """Validate parsed structured output with Kensa's response schema."""

    if result.parsed is None:
        raise _LLMStructuredOutputError("LLM response did not include parsed structured output.")
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
    raise _LLMStructuredOutputError("LLM response did not include parsed structured output.")


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
