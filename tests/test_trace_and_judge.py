from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from kensa import KensaTimeoutError, record_span
from kensa.conversation import ConversationResult, Termination
from kensa.judge import JudgeResult, judge, set_judge_provider
from kensa.llm import LLMResult
from kensa.runtime import KensaTrial, KensaTrialRuntime, reset_current_runtime, set_current_runtime


def test_judge_receives_conversation_result_as_json() -> None:
    calls: list[dict[str, Any]] = []

    class Provider:
        def judge(self, **kwargs: Any) -> JudgeResult:
            calls.append(kwargs)
            return JudgeResult(True, "structured")

    class TypedOutput(BaseModel):
        status: str

    set_judge_provider(Provider())
    try:
        result = judge(
            ConversationResult(
                messages=(
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "done"},
                ),
                output=TypedOutput(status="resolved"),
                termination=Termination(source="agent", reason="resolved"),
            ),
            "must resolve",
        )
    finally:
        set_judge_provider(None)

    assert result.passed
    assert calls[0]["output"] == {
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "done"},
        ],
        "output": {"status": "resolved"},
        "termination": {"source": "agent", "reason": "resolved"},
    }


def test_trace_spans_are_available_immediately_after_case_run(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import pytest
from kensa.pytest import ConversationResponse
from kensa.pytest import ConversationResponse
from kensa.tracing import record_tool_call


@pytest.fixture
def kensa_run():
    class Agent:
        def respond(self, messages):
            with record_tool_call("lookup_customer"):
                pass
            with record_tool_call("lookup_customer"):
                pass
            return ConversationResponse(output={"ok": True})
    return Agent()
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import ConversationResponse, kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run, kensa_trace):
    output = case.run(kensa_run)
    assert output.output == {"ok": True}
    assert not hasattr(kensa_trace, "called")
    assert kensa_trace.tools.include(["lookup_customer"])
    assert kensa_trace.tools.exclude(["missing"])
    assert kensa_trace.tools.order(["lookup_customer", "lookup_customer"])
    assert not kensa_trace.tools.order(["missing", "lookup_customer"])
    assert not kensa_trace.tools.no_repeats()
    assert kensa_trace.tools.names == ["lookup_customer", "lookup_customer"]
    assert kensa_trace.duration_ms >= 0
"""
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=1)


def test_force_flush_failure_exposes_incomplete_trace_state(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import pytest
from kensa.pytest import ConversationResponse
from kensa.pytest import ConversationResponse
from opentelemetry import trace
from kensa.tracing import record_tool_call


@pytest.fixture
def kensa_run(monkeypatch):
    class Agent:
        def respond(self, messages):
            provider = trace.get_tracer_provider()
            monkeypatch.setattr(provider, "force_flush", lambda timeout_millis=None: False)
            with record_tool_call("lookup_customer"):
                pass
            return ConversationResponse(content="ok")
    return Agent()
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import ConversationResponse
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run, kensa_trace):
    case.run(kensa_run)
    assert kensa_trace.incomplete
    assert "force_flush" in kensa_trace.incomplete_reason
"""
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=1)


def test_direct_kensa_run_does_not_record_output_artifact(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import pytest
from kensa.pytest import ConversationResponse


@pytest.fixture
def kensa_run():
    class Agent:
        def respond(self, messages):
            return ConversationResponse(output={"ok": True})
    return Agent()
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import ConversationResponse, kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run):
    assert kensa_run.respond(()) == ConversationResponse(output={"ok": True})
"""
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(passed=1)
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    assert payload["trials"][0]["output"] is None


def test_judge_result_can_be_asserted_and_is_recorded(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KENSA_JUDGE_RESULT", "pass")
    pytester.makeconftest(
        """
import pytest
from kensa.pytest import ConversationResponse


@pytest.fixture
def kensa_run():
    class Agent:
        def respond(self, messages):
            return ConversationResponse(content="safe")
    return Agent()
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import ConversationResponse
from kensa.pytest import judge, kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run):
    output = case.run(kensa_run)
    result = judge(output, "must be safe", input=case.input)
    assert result.passed, result.reasoning
"""
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(passed=1)
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    assert payload["trials"][0]["judges"][0]["passed"] is True


def test_judge_failure_reasoning_appears_in_assertion_output(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KENSA_JUDGE_RESULT", "fail")
    pytester.makeconftest(
        """
import pytest
from kensa.pytest import ConversationResponse


@pytest.fixture
def kensa_run():
    class Agent:
        def respond(self, messages):
            return ConversationResponse(content="unsafe")
    return Agent()
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import ConversationResponse
from kensa.pytest import judge, kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run):
    result = judge(case.run(kensa_run), "must be safe")
    assert result.passed, result.reasoning
"""
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Environment judge returned fail*"])


def test_no_judge_returns_explicit_error_result(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import pytest
from kensa.pytest import ConversationResponse


@pytest.fixture
def kensa_run():
    class Agent:
        def respond(self, messages):
            return ConversationResponse(content="safe")
    return Agent()
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import ConversationResponse
from kensa.pytest import judge, kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run):
    result = judge(case.run(kensa_run), "must be safe")
    assert not result.passed
    assert result.error
    assert "no-judge" in result.reasoning
"""
    )

    result = pytester.runpytest("-q", "--kensa-no-judge")

    result.assert_outcomes(passed=1)


def test_judge_provider_errors_are_explicit_results(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KENSA_JUDGE_RESULT", "error")
    pytester.makeconftest(
        """
import pytest
from kensa.pytest import ConversationResponse


@pytest.fixture
def kensa_run():
    class Agent:
        def respond(self, messages):
            return ConversationResponse(content="safe")
    return Agent()
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import judge, kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run):
    result = judge(case.run(kensa_run), "must be safe")
    assert not result.passed
    assert result.error
    assert "KENSA_JUDGE_RESULT=error" in result.reasoning
"""
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(passed=1)
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    assert payload["trials"][0]["judges"][0]["error"] is True


def test_judge_uses_builtin_llm_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_complete(
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        provider: str | None = None,
        temperature: float | None = None,
        response_format: Any = None,
        metadata: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> LLMResult:
        calls.append(
            {
                "messages": messages,
                "model": model,
                "provider": provider,
                "temperature": temperature,
                "response_format": response_format,
                "metadata": metadata,
                "timeout_s": timeout_s,
            }
        )
        payload = {
            "passed": True,
            "reasoning": "The output satisfies the criteria.",
            "evidence": ["safe response"],
        }
        return LLMResult(
            content=json.dumps(payload),
            provider=provider,
            model=model,
            metadata=metadata or {},
            parsed=payload,
        )

    set_judge_provider(None)
    monkeypatch.delenv("KENSA_JUDGE_RESULT", raising=False)
    monkeypatch.setenv("KENSA_JUDGE_MODEL", "gpt-5.4-mini")
    monkeypatch.setenv("KENSA_JUDGE_PROVIDER", "openai")
    judge_module = importlib.import_module("kensa.judge")
    monkeypatch.setattr(judge_module, "complete", fake_complete)

    result = judge("safe response", "must be safe", input="hello")

    assert result.passed
    assert result.provider == "openai"
    assert result.model == "gpt-5.4-mini"
    assert result.evidence == ["safe response"]
    assert calls[0]["model"] == "gpt-5.4-mini"
    assert calls[0]["provider"] == "openai"
    assert calls[0]["timeout_s"] == 30
    assert calls[0]["response_format"].__name__ == "_JudgeLLMResponse"
    system_message = calls[0]["messages"][0]
    assert system_message["role"] == "system"
    assert system_message["content"].startswith("You are a judge for AI agent evaluations.")
    assert "evaluations_judge" not in system_message["content"]
    assert "Set passed=false when required behavior is missing" in system_message["content"]
    assert "Do not include extra fields" in system_message["content"]


def test_judge_timeout_is_advisory_and_reports_active_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations: list[dict[str, Any] | None] = []
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test.py::test_agent[trial1]",
        group_id="test.py::test_agent",
        case_id="case",
        no_judge=False,
        judge_timeout_s=0.25,
        operation_callback=lambda operation: operations.append(
            operation.to_dict() if operation is not None else None
        ),
    )

    def timed_out(*args: Any, **kwargs: Any) -> LLMResult:
        del args, kwargs
        raise KensaTimeoutError("provider request timed out")

    set_judge_provider(None)
    monkeypatch.setenv("KENSA_JUDGE_MODEL", "gpt-5.4-mini")
    monkeypatch.setenv("KENSA_JUDGE_PROVIDER", "openai")
    monkeypatch.setattr("kensa.judge.complete", timed_out)
    token = set_current_runtime(runtime)
    try:
        result = judge("safe response", "must be safe")
    finally:
        reset_current_runtime(token)

    assert not result.passed
    assert result.error
    assert result.reasoning == "Judge timed out after 0.25 seconds"
    assert result.provider == "openai"
    assert result.model == "gpt-5.4-mini"
    assert result.metadata == {"timeout_s": 0.25}
    assert runtime.judges == [result]
    assert operations == [
        {
            "name": "judge",
            "attributes": {"provider": "openai", "model": "gpt-5.4-mini"},
        },
        None,
    ]


def test_custom_judge_provider_receives_deadline() -> None:
    observed: list[float] = []

    class Provider:
        def judge(self, **kwargs: Any) -> JudgeResult:
            observed.append(kwargs["timeout_s"])
            return JudgeResult(passed=True, reasoning="ok")

    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test.py::test_agent[trial1]",
        group_id="test.py::test_agent",
        case_id="case",
        no_judge=False,
        judge_timeout_s=0.75,
    )
    set_judge_provider(Provider())
    token = set_current_runtime(runtime)
    try:
        result = judge("safe response", "must be safe")
    finally:
        reset_current_runtime(token)
        set_judge_provider(None)

    assert result.passed
    assert observed == [0.75]


def test_overlapping_operations_publish_newest_remaining_operation() -> None:
    operations: list[str | None] = []
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test.py::test_agent[trial1]",
        group_id="test.py::test_agent",
        case_id="case",
        no_judge=False,
        operation_callback=lambda operation: operations.append(
            operation.name if operation is not None else None
        ),
    )

    async def exercise() -> None:
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        finish_first = asyncio.Event()
        finish_second = asyncio.Event()

        async def first() -> None:
            with record_span("first"):
                first_started.set()
                await finish_first.wait()

        async def second() -> None:
            await first_started.wait()
            with record_span("second"):
                second_started.set()
                await finish_second.wait()

        token = set_current_runtime(runtime)
        try:
            first_task = asyncio.create_task(first())
            second_task = asyncio.create_task(second())
            await second_started.wait()
            finish_first.set()
            await first_task
            finish_second.set()
            await second_task
        finally:
            reset_current_runtime(token)

    asyncio.run(exercise())

    assert operations == ["first", "second", "second", None]


def test_judge_timeout_before_provider_resolution_is_advisory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kensa.judge._provider_from_environment",
        lambda: (_ for _ in ()).throw(TimeoutError()),
    )

    result = judge("safe response", "must be safe")

    assert result.error
    assert result.provider is None
    assert result.reasoning == "Judge timed out after 30 seconds"
