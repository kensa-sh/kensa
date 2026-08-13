from __future__ import annotations

import asyncio
import importlib
import json
import traceback
from pathlib import Path
from typing import Any, cast

import pytest
from opentelemetry import trace
from pydantic import BaseModel, ValidationError

import kensa.conversation as conversation
import kensa.runtime as runtime_module
from kensa import KensaTimeoutError, record_llm_call, record_span, record_tool_call
from kensa.case import KensaMessage, kensa_case
from kensa.conversation import ConversationResponse, LLMSimulator, Termination
from kensa.errors import KensaEvalError, TrialFailure
from kensa.judge import JudgeResult, judge, set_judge_provider
from kensa.llm import LLMResult
from kensa.pytest import CaseResult, ToolCallEvidence
from kensa.runtime import (
    KensaSpan,
    KensaTrace,
    KensaTrial,
    KensaTrialRuntime,
    reset_current_runtime,
    set_current_runtime,
)
from kensa.target import (
    AgentEvent,
    AgentRunEvidence,
    EffectPolicy,
    EvidenceCompleteness,
    ExecutionAttestation,
    attach_agent_run,
)


def test_tool_call_evidence_is_strict_frozen_and_isolated() -> None:
    import kensa.pytest as kensa_pytest

    arguments = {"nested": {"ids": [1]}}
    result = {"found": True}

    call = ToolCallEvidence(
        sequence=0,
        name="lookup",
        arguments=arguments,
        result=result,
        arguments_recorded=True,
        result_recorded=True,
        status="ok",
        span_id="span-1",
        duration_ms=1.5,
    )
    arguments["nested"]["ids"].append(2)
    result["found"] = False

    assert "ToolCallEvidence" in kensa_pytest.__all__
    assert call.arguments == {"nested": {"ids": [1]}}
    assert call.result == {"found": True}
    with pytest.raises(ValidationError, match="frozen_instance"):
        call.status = "error"
    with pytest.raises(ValidationError):
        ToolCallEvidence.model_validate(
            {
                **call.model_dump(),
                "sequence": True,
            }
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ToolCallEvidence.model_validate(
            {
                **call.model_dump(),
                "unexpected": True,
            }
        )


def test_tool_calls_normalize_payload_sources_presence_order_and_serialization() -> None:
    trace_view = KensaTrace()
    trace_view.replace(
        [
            KensaSpan(name="llm", kind="llm"),
            KensaSpan(name="missing", kind="tool", tool_name="missing", span_id="span-0"),
            KensaSpan(
                name="canonical",
                kind="tool",
                tool_name="lookup",
                span_id="span-1",
                start_time_unix_nano=10,
                end_time_unix_nano=2_000_010,
                attributes={
                    "kensa.tool.args": "null",
                    "kensa.tool.result": '{"found": true}',
                    "arguments": '{"ignored": true}',
                    "result": '{"ignored": true}',
                },
            ),
            KensaSpan(
                name="attached",
                kind="tool",
                tool_name="lookup",
                span_id="span-2",
                status="error",
                attributes={
                    "kensa.evidence.source": "agent_run",
                    "input": {"nested": {"value": 1}},
                    "output": None,
                    "arguments": '{"ignored": true}',
                    "result": '{"ignored": true}',
                },
            ),
            KensaSpan(
                name="legacy",
                kind="tool",
                tool_name="legacy",
                span_id=None,
                attributes={
                    "arguments": '{"nested": {"value": 1}}',
                    "result": "NaN",
                },
            ),
        ]
    )

    calls = trace_view.tools.calls

    assert isinstance(calls, tuple)
    assert [call.sequence for call in calls] == list(range(len(calls)))
    assert (
        trace_view.tools.names
        == [call.name for call in calls]
        == [
            "missing",
            "lookup",
            "lookup",
            "legacy",
        ]
    )
    assert calls[0].arguments is None
    assert calls[0].result is None
    assert calls[0].arguments_recorded is False
    assert calls[0].result_recorded is False
    assert calls[1].arguments is None
    assert calls[1].arguments_recorded is True
    assert calls[1].result == {"found": True}
    assert calls[1].result_recorded is True
    assert calls[1].duration_ms == 2.0
    assert calls[2].arguments == {"nested": {"value": 1}}
    assert calls[2].result is None
    assert calls[2].result_recorded is True
    assert calls[2].status == "error"
    assert calls[3].arguments == {"nested": {"value": 1}}
    assert calls[3].result == "NaN"
    assert trace_view.tools.include(["lookup", "legacy"])
    assert trace_view.tools.exclude(["other"])
    assert trace_view.tools.order(["missing", "lookup", "legacy"])
    assert not trace_view.tools.no_repeats()

    serialized = trace_view.to_dict()
    assert serialized["tools"] == trace_view.tools.names
    assert serialized["tool_calls"] == [call.model_dump(mode="json") for call in calls]

    trace_view.replace(list(trace_view.spans))
    assert trace_view.tools.calls == calls
    assert [call.sequence for call in trace_view.tools.calls] == list(range(len(calls)))


def test_tool_call_matching_uses_recursive_json_subset_rules() -> None:
    trace_view = KensaTrace()
    trace_view.replace(
        [
            KensaSpan(
                name="first",
                kind="tool",
                tool_name="lookup",
                status="ok",
                attributes={
                    "arguments": {
                        "nested": {"value": 1, "extra": "kept"},
                        "items": [{"id": 1}],
                        "flag": True,
                    },
                    "result": {"found": True, "count": 1},
                },
            ),
            KensaSpan(
                name="second",
                kind="tool",
                tool_name="lookup",
                status="error",
                attributes={
                    "arguments": {"nested": {"value": 2}},
                    "result": {"found": False},
                },
            ),
            KensaSpan(name="missing", kind="tool", tool_name="lookup"),
            KensaSpan(
                name="scalar",
                kind="tool",
                tool_name="lookup",
                attributes={"arguments": "not-json"},
            ),
        ]
    )

    first, second, missing, scalar = trace_view.tools.calls

    assert trace_view.tools.matching("lookup") == (first, second, missing, scalar)
    assert trace_view.tools.matching(
        "lookup",
        arguments={"nested": {"value": 1}},
    ) == (first,)
    assert trace_view.tools.matching("lookup", arguments={}) == (first, second)
    assert trace_view.tools.matching(
        "lookup",
        arguments={"items": [{"id": 1}]},
    ) == (first,)
    assert (
        trace_view.tools.matching(
            "lookup",
            arguments={"items": [{"id": 1, "extra": True}]},
        )
        == ()
    )
    assert trace_view.tools.matching("lookup", arguments={"items": []}) == ()
    assert trace_view.tools.matching("lookup", arguments={"flag": 1}) == ()
    assert trace_view.tools.matching("lookup", result={"found": True}) == (first,)
    assert trace_view.tools.matching("lookup", status="error") == (second,)
    assert trace_view.tools.called(
        "lookup",
        arguments={"nested": {"value": 2}},
        result={"found": False},
        status="error",
    )
    assert not trace_view.tools.called("lookup", result={"missing": True})

    with pytest.raises(TypeError, match="arguments"):
        trace_view.tools.matching("lookup", arguments=cast(Any, []))
    with pytest.raises(TypeError, match="result"):
        trace_view.tools.called("lookup", result=cast(Any, "found"))
    with pytest.raises(TypeError, match="strict JSON object"):
        trace_view.tools.matching(
            "lookup",
            arguments=cast(Any, {"invalid": object()}),
        )


def test_recorded_tool_call_snapshots_live_evidence_and_preserves_tool_error() -> None:
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test.py::test_agent[trial1]",
        group_id="test.py::test_agent",
        case_id="case",
        no_judge=False,
    )
    arguments = {"customer": {"id": "cus_1"}}
    result = {"found": True}

    def successful_operation() -> str:
        with record_tool_call("lookup", arguments=arguments) as tool_call:
            tool_call.set_result(result)
            arguments["customer"]["id"] = "mutated"
            result["found"] = False
        return "done"

    token = set_current_runtime(runtime)
    try:
        runtime.run_case(kensa_case(id="success", input="hello"), successful_operation)
    finally:
        reset_current_runtime(token)

    call = runtime.trace.tools.calls[0]
    assert call.arguments == {"customer": {"id": "cus_1"}}
    assert call.result == {"found": True}
    assert call.status == "ok"

    failed_runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test.py::test_agent[trial1]",
        group_id="test.py::test_agent",
        case_id="case",
        no_judge=False,
    )
    error = RuntimeError("tool failed")

    def failed_operation() -> None:
        with record_tool_call("lookup", arguments={"customer_id": "cus_1"}):
            raise error

    token = set_current_runtime(failed_runtime)
    try:
        with pytest.raises(RuntimeError, match="tool failed") as raised:
            failed_runtime.run_case(
                kensa_case(id="failure", input="hello"),
                failed_operation,
            )
    finally:
        reset_current_runtime(token)

    assert raised.value is error
    assert any(
        frame.name == "failed_operation" for frame in traceback.extract_tb(error.__traceback__)
    )
    failed_call = failed_runtime.trace.tools.calls[0]
    assert failed_call.status == "error"
    assert failed_call.result is None
    assert failed_call.result_recorded is False


def test_judge_require_distinguishes_verdict_execution_and_contract_failures() -> None:
    passed = JudgeResult(passed=True, reasoning="ok", metadata={"nested": ["safe"]})
    assert passed.require() is passed

    with pytest.raises(AssertionError, match="criteria not met"):
        JudgeResult(passed=False, reasoning="criteria not met").require()

    execution = JudgeResult(
        passed=False,
        reasoning="provider unavailable",
        provider="openai",
        model="gpt-test",
        metadata={"request_id": "req-1"},
        error=True,
    )
    with pytest.raises(KensaEvalError) as execution_error:
        execution.require()
    assert execution_error.value.failure.model_dump(mode="json") == {
        "category": "judge",
        "kind": "execution",
        "message": "provider unavailable",
        "evidence": {
            "provider": "openai",
            "model": "gpt-test",
            "metadata": {"request_id": "req-1"},
        },
    }

    snapshots: list[dict[str, Any]] = []
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test.py::test_agent[trial1]",
        group_id="test.py::test_agent",
        case_id="case",
        no_judge=False,
        snapshot_callback=lambda current: snapshots.append(
            current.metadata(status="provisional", duration_ms=0).to_dict()
        ),
    )
    runtime.output_recorded = True

    class InvalidResultProvider:
        def judge(self, **kwargs: Any) -> JudgeResult:
            del kwargs
            return JudgeResult(
                passed=cast(Any, "yes"),
                reasoning="invalid",
                provider="custom",
                model="judge-v1",
            )

    set_judge_provider(InvalidResultProvider())
    token = set_current_runtime(runtime)
    try:
        invalid = judge("output", "criteria")
    finally:
        reset_current_runtime(token)
        set_judge_provider(None)

    assert invalid.to_dict() == {
        "passed": False,
        "reasoning": "Judge result violates the JudgeResult contract.",
        "evidence": [],
        "provider": "custom",
        "model": "judge-v1",
        "metadata": {},
        "error": True,
    }
    assert snapshots[-1]["judges"] == [
        {
            "id": "judge-1",
            "criteria": "criteria",
            "required": False,
            **invalid.to_dict(),
            "error_kind": "contract",
        }
    ]
    with pytest.raises(KensaEvalError) as contract_error:
        invalid.require()
    assert contract_error.value.failure.model_dump(mode="json") == {
        "category": "judge",
        "kind": "contract",
        "message": "Judge result violates the JudgeResult contract.",
        "evidence": {"provider": "custom", "model": "judge-v1"},
    }
    assert isinstance(contract_error.value.__cause__, ValidationError)

    direct_invalid = JudgeResult(
        passed=False,
        reasoning="invalid",
        provider="direct",
        metadata={"invalid": object()},
        error=True,
    )
    with pytest.raises(KensaEvalError) as direct_error:
        direct_invalid.require()
    assert direct_error.value.failure.category == "judge"
    assert direct_error.value.failure.kind == "contract"
    assert isinstance(direct_error.value.__cause__, TypeError)

    with pytest.raises(KensaEvalError) as blank_reasoning_error:
        JudgeResult(passed=False, reasoning="", error=True).require()
    assert blank_reasoning_error.value.failure.category == "judge"
    assert blank_reasoning_error.value.failure.kind == "contract"
    assert isinstance(blank_reasoning_error.value.__cause__, ValidationError)

    class NonObjectJudgeResult(JudgeResult):
        def to_dict(self) -> dict[str, Any]:
            return cast(Any, [])

    with pytest.raises(KensaEvalError) as non_object_error:
        NonObjectJudgeResult(passed=True, reasoning="invalid").require()
    assert isinstance(non_object_error.value.__cause__, TypeError)


def test_judge_require_defers_only_results_recorded_by_the_active_trial() -> None:
    class Engine:
        def start_case(self, evaluation_id: str, case: dict[str, Any]) -> None:
            assert evaluation_id.endswith("::trial1")
            assert case["id"] == "case"

    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test.py::test_agent[trial1]",
        group_id="test.py::test_agent",
        case_id="case",
        no_judge=False,
        engine=cast(Any, Engine()),
    )

    class Provider:
        def judge(self, **kwargs: Any) -> JudgeResult:
            return JudgeResult(False, f"failed {kwargs['criteria']}")

    set_judge_provider(Provider())
    token = set_current_runtime(runtime)
    try:
        runtime.run_case(kensa_case(id="case", input="hello"), lambda: "output")
        recorded = judge("output", "grounded answer")
        assert recorded.require() is recorded
        recorded.metadata["changed_after_recording"] = True
        with pytest.raises(AssertionError, match="not recorded"):
            JudgeResult(False, "not recorded").require()
        copied = JudgeResult(**recorded.to_dict())
        with pytest.raises(AssertionError, match="failed grounded answer"):
            copied.require()
    finally:
        reset_current_runtime(token)
        set_judge_provider(None)

    inactive = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test.py::test_inactive[trial1]",
        group_id="test.py::test_inactive",
        case_id="case",
        no_judge=False,
    )
    inactive.record_judge(recorded, criteria="grounded answer")
    inactive_token = set_current_runtime(inactive)
    try:
        with pytest.raises(AssertionError, match="failed grounded answer"):
            recorded.require()
    finally:
        reset_current_runtime(inactive_token)

    assert runtime.metadata(status="provisional", duration_ms=0).judges == [
        {
            "id": "judge-1",
            "criteria": "grounded answer",
            "required": True,
            "passed": False,
            "reasoning": "failed grounded answer",
            "evidence": [],
            "provider": None,
            "model": None,
            "metadata": {},
            "error": False,
            "error_kind": None,
        }
    ]


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


def test_instrumented_genai_spans_are_llm_turns_with_partial_cost() -> None:
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test_instrumented_genai_spans",
        group_id="group",
        case_id="case",
        no_judge=False,
    )

    def operation() -> dict[str, bool]:
        tracer = trace.get_tracer("instrumented-genai")
        with tracer.start_as_current_span(
            "current.chat",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "openai",
                "kensa.cost_usd": 0.2,
            },
        ):
            pass
        with tracer.start_as_current_span(
            "legacy.chat",
            attributes={"gen_ai.system": "openai"},
        ):
            pass
        with tracer.start_as_current_span(
            "current.tool",
            attributes={"gen_ai.tool.name": "lookup"},
        ):
            pass
        return {"ok": True}

    runtime.run_case(kensa_case(id="instrumented", input="hello"), operation)

    llm_spans = [span for span in runtime.trace.spans if span.kind == "llm"]
    assert [span.name for span in llm_spans] == ["current.chat", "legacy.chat"]
    assert runtime.trace.llm_turns == 2
    assert runtime.trace.known_cost_usd == 0.2
    assert runtime.trace.cost_available is False
    assert runtime.trace.cost_usd is None
    assert runtime.trace.tools.names == ["lookup"]


def test_attached_evidence_survives_snapshots_errors_and_flush_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots: list[dict[str, Any]] = []
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test_attached_evidence_survives_snapshots_errors_and_flush_failure",
        group_id="group",
        case_id="case",
        no_judge=True,
        snapshot_callback=lambda current: snapshots.append(current.trace.to_dict()),
    )
    evidence = AgentRunEvidence(
        run_id="external-run",
        attestation=ExecutionAttestation(
            revision="revision-1",
            environment="sandbox",
            effects=EffectPolicy.SANDBOXED,
        ),
        events=(
            AgentEvent(
                id="external-tool",
                sequence=1,
                kind="tool",
                name="external.lookup",
                status="completed",
            ),
        ),
        trajectory_completeness=EvidenceCompleteness.COMPLETE,
        state_completeness=EvidenceCompleteness.COMPLETE,
    )
    trace_identity = runtime.trace

    class FailingProvider:
        def force_flush(self, *, timeout_millis: int) -> bool:
            assert timeout_millis == 10_000
            raise RuntimeError("flush unavailable")

    def operation() -> None:
        attach_agent_run(evidence)
        monkeypatch.setattr(runtime_module.trace, "get_tracer_provider", FailingProvider)
        raise RuntimeError("target failed")

    token = set_current_runtime(runtime)
    try:
        with pytest.raises(RuntimeError, match="target failed"):
            runtime.run_case(kensa_case(id="failure", input="hello"), operation)
        runtime._record_conversation_snapshot({"accepted": True})
        runtime.record_judge(JudgeResult(True, "preserved"), criteria="Preserve this judge")
        metadata = runtime.metadata(
            status="error",
            duration_ms=1,
            failure=TrialFailure(
                category="agent",
                kind="execution",
                message="target failed",
            ),
        )
    finally:
        reset_current_runtime(token)

    assert runtime.trace is trace_identity
    assert runtime.trace.incomplete is True
    assert runtime.trace.incomplete_reason == "OpenTelemetry force_flush failed: flush unavailable"
    assert [run.run_id for run in runtime.trace.agent_runs] == ["external-run"]
    assert sum(span.span_id == "external-tool" for span in runtime.trace.spans) == 1
    assert metadata.trace["agent_runs"][0]["run_id"] == "external-run"
    assert all(snapshot["agent_runs"][0]["run_id"] == "external-run" for snapshot in snapshots)


def test_completed_llm_operation_publishes_trace_before_case_output() -> None:
    snapshots: list[tuple[bool, int, float | None]] = []
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test_completed_llm_snapshot",
        group_id="group",
        case_id="case",
        no_judge=False,
        snapshot_callback=lambda current: snapshots.append(
            (
                current.output_recorded,
                current.trace.llm_turns,
                current.trace.known_cost_usd,
            )
        ),
    )

    def operation() -> str:
        with record_llm_call(attributes={"kensa.cost_usd": 0.2}):
            pass
        return "done"

    token = set_current_runtime(runtime)
    try:
        runtime.run_case(kensa_case(id="snapshot", input="hello"), operation)
    finally:
        reset_current_runtime(token)

    assert snapshots == [(False, 1, 0.2), (True, 1, 0.2)]


def test_completed_instrumented_genai_span_publishes_trace_before_case_output() -> None:
    snapshots: list[tuple[bool, int, float | None]] = []
    operation_kinds: list[str | None] = []
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test_completed_instrumented_genai_snapshot",
        group_id="group",
        case_id="case",
        no_judge=False,
        operation_callback=lambda operation: operation_kinds.append(
            operation.kind if operation is not None else None
        ),
        snapshot_callback=lambda current: snapshots.append(
            (
                current.output_recorded,
                current.trace.llm_turns,
                current.trace.known_cost_usd,
            )
        ),
    )

    def operation() -> str:
        tracer = trace.get_tracer("instrumented-genai")
        with tracer.start_as_current_span(
            "chat test-model",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "test",
                "gen_ai.request.model": "test-model",
                "kensa.cost_usd": 0.2,
            },
        ):
            pass
        return "done"

    token = set_current_runtime(runtime)
    try:
        runtime.run_case(kensa_case(id="snapshot", input="hello"), operation)
    finally:
        reset_current_runtime(token)

    assert operation_kinds == ["llm", None]
    assert snapshots == [(False, 1, 0.2), (True, 1, 0.2)]


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
    assert not hasattr(result.trace, "called")
    assert result.trace.tools.include(["lookup_customer"])
    assert result.trace.tools.exclude(["missing"])
    assert result.trace.tools.order(["lookup_customer", "lookup_customer"])
    assert not result.trace.tools.order(["missing", "lookup_customer"])
    assert not result.trace.tools.no_repeats()
    assert result.trace.tools.names == ["lookup_customer", "lookup_customer"]
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
def test_agent(case, kensa_run):
    result = case.run(kensa_run)
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
            "kind": "span",
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
