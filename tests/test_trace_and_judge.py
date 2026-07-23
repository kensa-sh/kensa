from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace
from pydantic import BaseModel

import kensa.conversation as conversation
from kensa import KensaTimeoutError, record_llm_call, record_span, record_tool_call
from kensa.case import KensaMessage, kensa_case
from kensa.conversation import ConversationResponse, LLMSimulator, Termination
from kensa.judge import JudgeResult, judge, set_judge_provider
from kensa.llm import LLMResult
from kensa.pytest import CaseResult
from kensa.runtime import KensaTrial, KensaTrialRuntime, reset_current_runtime, set_current_runtime


def test_judge_receives_case_result_as_json() -> None:
    calls: list[dict[str, Any]] = []

    class Provider:
        def judge(self, **kwargs: Any) -> JudgeResult:
            calls.append(kwargs)
            return JudgeResult(True, "structured")

    class TypedOutput(BaseModel):
        status: str

    case_result = CaseResult(
        messages=(
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "done"},
        ),
        output=TypedOutput(status="resolved"),
        termination=Termination(source="agent", reason="resolved"),
    )
    set_judge_provider(Provider())
    try:
        result = judge(case_result, "must resolve")
        explicit_result = judge(
            case_result,
            "must resolve",
            trace=case_result.trace,
        )
    finally:
        set_judge_provider(None)

    assert result.passed
    assert explicit_result.passed
    assert calls[0]["output"] == {
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "done"},
        ],
        "output": {"status": "resolved"},
        "termination": {"source": "agent", "reason": "resolved"},
    }
    assert calls[0]["trace"] is None
    assert calls[1]["output"] == calls[0]["output"]
    assert calls[1]["trace"] is case_result.trace


@pytest.mark.asyncio
async def test_response_spans_attribute_sources_without_filtering_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_acomplete(messages: list[dict[str, Any]], **kwargs: Any) -> LLMResult:
        del messages, kwargs
        trace.get_current_span().set_attribute("kensa.cost_usd", 0.5)
        return LLMResult(
            content="next",
            parsed={"content": "next", "termination_reason": None},
        )

    class Agent:
        def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            with record_llm_call("agent.llm", attributes={"kensa.cost_usd": 0.25}):
                pass
            with record_tool_call("agent.tool"):
                pass
            return ConversationResponse(content="done", termination_reason="done")

    monkeypatch.setattr(conversation, "acomplete", fake_acomplete)
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test_response_spans_attribute_sources_without_filtering_totals",
        group_id="group",
        case_id="case",
        no_judge=False,
    )
    token = set_current_runtime(runtime)
    try:
        result = await kensa_case(id="trace", input="x").run(
            Agent(),
            simulator=LLMSimulator("customer"),
            max_turns=1,
        )
    finally:
        reset_current_runtime(token)

    assert result.trace is runtime.trace
    assert result.trace.spans
    response_spans = {
        span.attributes["kensa.conversation.source"]: span
        for span in runtime.trace.spans
        if span.name == "kensa.conversation.respond"
    }
    simulator_llm = next(span for span in runtime.trace.spans if span.name == "llm.call")
    agent_llm = next(span for span in runtime.trace.spans if span.name == "agent.llm")
    agent_tool = next(span for span in runtime.trace.spans if span.name == "agent.tool")

    assert simulator_llm.parent_span_id == response_spans["simulator"].span_id
    assert agent_llm.parent_span_id == response_spans["agent"].span_id
    assert agent_tool.parent_span_id == response_spans["agent"].span_id
    assert runtime.trace.cost_usd == 0.75
    assert runtime.trace.llm_turns == 2
    assert runtime.trace.tools.names == ["agent.tool"]
    assert runtime.trace.duration_ms >= 0


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
    result = case.run(kensa_run)
    assert result.output == {"ok": True}
    assert result.trace is kensa_trace
    assert not hasattr(kensa_trace, "called")
    assert result.trace.tools.include(["lookup_customer"])
    assert result.trace.tools.exclude(["missing"])
    assert result.trace.tools.order(["lookup_customer", "lookup_customer"])
    assert not result.trace.tools.order(["missing", "lookup_customer"])
    assert not result.trace.tools.no_repeats()
    assert kensa_trace.tools.names == ["lookup_customer", "lookup_customer"]
    assert result.trace.duration_ms >= 0
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
    result = case.run(kensa_run)
    assert result.trace is kensa_trace
    assert result.trace.incomplete
    assert "force_flush" in result.trace.incomplete_reason
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
    result = case.run(kensa_run)
    verdict = judge(result, "must be safe", input=case.input)
    assert verdict.passed, verdict.reasoning
"""
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(passed=1)
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    trial = payload["trials"][0]
    assert trial["output"] == {
        "messages": [{"role": "assistant", "content": "safe"}],
        "output": "safe",
        "termination": {"source": "engine", "reason": "direct"},
    }
    assert trial["judges"][0]["passed"] is True


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
