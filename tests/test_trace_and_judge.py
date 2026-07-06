from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from kensa.judge import judge, set_judge_provider
from kensa.llm import LLMResult


def test_trace_spans_are_available_immediately_after_case_run(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import pytest
from kensa.tracing import record_tool_call


@pytest.fixture
def kensa_run():
    def _run(case):
        with record_tool_call("lookup_customer"):
            pass
        with record_tool_call("lookup_customer"):
            pass
        return {"ok": True}
    return _run
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run, kensa_trace):
    output = case.run(kensa_run)
    assert output == {"ok": True}
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
from opentelemetry import trace
from kensa.tracing import record_tool_call


@pytest.fixture
def kensa_run(monkeypatch):
    def _run(case):
        provider = trace.get_tracer_provider()
        monkeypatch.setattr(provider, "force_flush", lambda timeout_millis=None: False)
        with record_tool_call("lookup_customer"):
            pass
        return "ok"
    return _run
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
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


@pytest.fixture
def kensa_run():
    return lambda case: {"ok": True}
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run):
    assert kensa_run(case) == {"ok": True}
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


@pytest.fixture
def kensa_run():
    return lambda case: "safe"
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
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


@pytest.fixture
def kensa_run():
    return lambda case: "unsafe"
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


@pytest.fixture
def kensa_run():
    return lambda case: "safe"
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


@pytest.fixture
def kensa_run():
    return lambda case: "safe"
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
    ) -> LLMResult:
        calls.append(
            {
                "messages": messages,
                "model": model,
                "provider": provider,
                "temperature": temperature,
                "response_format": response_format,
                "metadata": metadata,
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
    assert calls[0]["response_format"].__name__ == "_JudgeLLMResponse"
    system_message = calls[0]["messages"][0]
    assert system_message["role"] == "system"
    assert system_message["content"].startswith("You are a judge for AI agent evaluations.")
    assert "evaluations_judge" not in system_message["content"]
    assert "Set passed=false when required behavior is missing" in system_message["content"]
    assert "Do not include extra fields" in system_message["content"]
