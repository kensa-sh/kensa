"""Judge helper for Kensa agent eval tests."""

from __future__ import annotations

import json
import os
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from kensa._serialization import json_value
from kensa.llm import DEFAULT_LLM_MODEL, complete, resolve_llm_config, validate_structured_result
from kensa.models import LLMModel
from kensa.runtime import current_runtime
from kensa.watchdog import DEFAULT_JUDGE_TIMEOUT_S, format_timeout_s, timeout_value

DEFAULT_ANTHROPIC_JUDGE_MODEL = LLMModel.CLAUDE_SONNET_4_6.value


class JudgeProvider(Protocol):
    def judge(
        self,
        *,
        output: Any,
        criteria: str,
        input: Any = None,
        trace: Any = None,
        context: Any = None,
        timeout_s: float = DEFAULT_JUDGE_TIMEOUT_S,
    ) -> JudgeResult: ...


@dataclass(frozen=True)
class JudgeResult:
    passed: bool
    reasoning: str
    evidence: list[str] = field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasoning": self.reasoning,
            "evidence": self.evidence,
            "provider": self.provider,
            "model": self.model,
            "metadata": self.metadata,
            "error": self.error,
        }


class _JudgeLLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    reasoning: str
    evidence: list[str] = Field(default_factory=list)


EVALUATIONS_JUDGE_SYSTEM_PROMPT = (
    "You are a judge for AI agent evaluations.\n"
    "Decide whether the observed output satisfies the criteria for the given input, "
    "trace, and context. Judge only behavior shown in the supplied payload; do not "
    "infer hidden intent, missing facts, or unobserved tool results. Treat the "
    "criteria as the source of truth. Set passed=true only when the output fully "
    "satisfies every explicit requirement and is not contradicted by the trace or "
    "context. Set passed=false when required behavior is missing, ambiguous, only "
    "partially satisfied, contradicted, or unsupported by evidence. Ground reasoning "
    "in concrete observations from the payload. Respond using the provided structured "
    "output schema only: passed must be boolean, reasoning must be a concise string, "
    "and evidence must be an array of strings. Do not include extra fields."
)


_PROVIDER: JudgeProvider | None = None


def set_judge_provider(provider: JudgeProvider | None) -> None:
    global _PROVIDER
    _PROVIDER = provider


def judge(
    output: Any,
    criteria: str,
    *,
    input: Any = None,
    trace: Any = None,
    context: Any = None,
) -> JudgeResult:
    """Run a semantic assertion helper and return an explicit result object."""

    runtime = current_runtime()
    timeout_s = runtime.judge_timeout_s if runtime is not None else DEFAULT_JUDGE_TIMEOUT_S
    if runtime is not None and runtime.no_judge:
        result = JudgeResult(
            passed=False,
            reasoning="Judge skipped because --kensa-no-judge is enabled.",
            evidence=[],
            provider="disabled",
            error=True,
        )
        runtime.record_judge(result)
        return result

    provider: JudgeProvider | None = None
    try:
        normalized_output = json_value(output)
        provider = _PROVIDER or _provider_from_environment()
        if provider is None:
            result = JudgeResult(
                passed=False,
                reasoning=(
                    "No local Kensa judge provider is configured. Set "
                    "KENSA_JUDGE_MODEL for LLM judging or KENSA_JUDGE_RESULT for local tests."
                ),
                provider="none",
                error=True,
            )
        else:
            operation = (
                runtime.operation("judge", _judge_operation_attributes(provider))
                if runtime is not None
                else nullcontext()
            )
            with operation:
                result = provider.judge(
                    output=normalized_output,
                    criteria=criteria,
                    input=input,
                    trace=trace,
                    context=context,
                    timeout_s=timeout_s,
                )
    except TimeoutError:
        provider_name, model = _judge_identity(provider)
        result = JudgeResult(
            passed=False,
            reasoning=f"Judge timed out after {format_timeout_s(timeout_s)} seconds",
            provider=provider_name,
            model=model,
            metadata={"timeout_s": timeout_value(timeout_s)},
            error=True,
        )
    except Exception as exc:
        result = JudgeResult(
            passed=False,
            reasoning=f"Judge error: {exc}",
            provider="error",
            error=True,
        )

    if runtime is not None:
        runtime.record_judge(result)
    return result


def _judge_operation_attributes(provider: JudgeProvider) -> dict[str, Any]:
    provider_name, model = _judge_identity(provider)
    attributes: dict[str, Any] = {}
    if provider_name is not None:
        attributes["provider"] = provider_name
    if model is not None:
        attributes["model"] = model
    return attributes


def _judge_identity(provider: JudgeProvider | None) -> tuple[str | None, str | None]:
    if isinstance(provider, _LLMJudge):
        return provider.config.provider.value, provider.config.model.value
    if isinstance(provider, _EnvJudge):
        return "env", "KENSA_JUDGE_RESULT"
    if provider is None:
        return None, None
    return type(provider).__name__, None


def _provider_from_environment() -> JudgeProvider | None:
    fake = os.environ.get("KENSA_JUDGE_RESULT")
    if fake:
        return _EnvJudge(fake)
    provider = os.environ.get("KENSA_JUDGE_PROVIDER") or os.environ.get("KENSA_LLM_PROVIDER")
    model = os.environ.get("KENSA_JUDGE_MODEL") or os.environ.get("KENSA_LLM_MODEL")
    if model is None and provider is not None:
        model = _default_judge_model_for_provider(provider)
    if (
        model is None
        and provider is None
        and not os.environ.get("OPENAI_API_KEY")
        and os.environ.get("ANTHROPIC_API_KEY")
    ):
        provider = "anthropic"
        model = DEFAULT_ANTHROPIC_JUDGE_MODEL
    return _LLMJudge(
        model=model or DEFAULT_LLM_MODEL,
        provider=provider,
    )


def _default_judge_model_for_provider(provider: str) -> str:
    if provider.strip().lower() == "anthropic":
        return DEFAULT_ANTHROPIC_JUDGE_MODEL
    return DEFAULT_LLM_MODEL


class _EnvJudge:
    def __init__(self, verdict: str) -> None:
        self.verdict = verdict.strip().lower()

    def judge(
        self,
        *,
        output: Any,
        criteria: str,
        input: Any = None,
        trace: Any = None,
        context: Any = None,
        timeout_s: float = DEFAULT_JUDGE_TIMEOUT_S,
    ) -> JudgeResult:
        del output, input, trace, context, timeout_s
        if self.verdict == "error":
            raise RuntimeError("KENSA_JUDGE_RESULT=error")
        passed = self.verdict in {"1", "true", "pass", "passed", "yes"}
        return JudgeResult(
            passed=passed,
            reasoning=f"Environment judge returned {'pass' if passed else 'fail'} for: {criteria}",
            evidence=[],
            provider="env",
            model="KENSA_JUDGE_RESULT",
        )


class _LLMJudge:
    def __init__(self, *, model: str, provider: str | None = None) -> None:
        self.config = resolve_llm_config(model=model, provider=provider)

    def judge(
        self,
        *,
        output: Any,
        criteria: str,
        input: Any = None,
        trace: Any = None,
        context: Any = None,
        timeout_s: float = DEFAULT_JUDGE_TIMEOUT_S,
    ) -> JudgeResult:
        payload = {
            "criteria": criteria,
            "input": input,
            "output": output,
            "trace": trace.to_dict() if hasattr(trace, "to_dict") else trace,
            "context": context,
        }
        result = complete(
            [
                {
                    "role": "system",
                    "content": EVALUATIONS_JUDGE_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, sort_keys=True, default=repr),
                },
            ],
            model=self.config.model,
            provider=self.config.provider,
            temperature=0.0,
            response_format=_JudgeLLMResponse,
            metadata={"task": "judge"},
            timeout_s=timeout_s,
        )
        data = validate_structured_result(result, _JudgeLLMResponse)
        return JudgeResult(
            passed=data.passed,
            reasoning=data.reasoning,
            evidence=data.evidence,
            provider=result.provider or self.config.provider.value,
            model=result.model or self.config.model.value,
            metadata=result.metadata,
            error=False,
        )


__all__ = ["JudgeProvider", "JudgeResult", "judge", "set_judge_provider"]
