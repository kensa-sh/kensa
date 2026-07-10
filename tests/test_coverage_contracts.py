from __future__ import annotations

import argparse
import asyncio
import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest

from kensa import cli, cli_output, cli_traces
from kensa.case import KensaCase, KensaCaseError, KensaMessage, kensa_case
from kensa.judge import JudgeResult, judge, set_judge_provider
from kensa.llm import DEFAULT_LLM_MODEL, LLMResult
from kensa.pytest_plugin import (
    PRIVATE_TRIAL,
    KensaAggregate,
    KensaSessionState,
    _case_id,
    _kensa_trial_fixture,
    _marker_trials,
    _runtime_for_item,
    pytest_make_parametrize_id,
    pytest_runtest_makereport,
    pytest_terminal_summary,
)
from kensa.pytest_plugin import kensa_trace as kensa_trace_fixture
from kensa.runtime import (
    KensaSpan,
    KensaTrace,
    KensaTrial,
    KensaTrialRuntime,
    TrialMetadata,
    collect_spans,
    current_runtime,
    ensure_tracing,
    reset_current_runtime,
    set_current_runtime,
)
from kensa.tracing import JSONLSpanExporter, instrument


def test_case_fallbacks_and_uninstrumented_run_paths() -> None:
    assert (
        KensaCase("direct_input", MappingProxyType({"id": "direct_input", "input": "x"})).input
        == "x"
    )
    assert KensaCase(
        "direct_messages",
        MappingProxyType({"id": "direct_messages", "messages": [1]}),
    ).input == [1]
    assert kensa_case(id="single", customer="c1").input == "c1"
    assert kensa_case(id="multi", customer="c1", region="us").input == {
        "customer": "c1",
        "region": "us",
    }
    messages: list[KensaMessage] = [{"role": "user", "content": "hello"}]
    assert kensa_case(id="messages", messages=messages).messages == messages
    with pytest.raises(KensaCaseError, match=r"messages=\.\.\."):
        _ = kensa_case(id="raw_input", input=messages).messages
    with pytest.raises(KensaCaseError, match="messages"):
        _ = kensa_case(id="no_messages", input="hello").messages
    with pytest.raises(KensaCaseError, match="Use either input"):
        kensa_case(id="bad", input="hello", messages=messages)

    case = kensa_case(id="run", input="hello")
    assert repr(case) == "run"
    assert case.run(lambda c: {"seen": c.input}) == {"seen": "hello"}
    with pytest.raises(KensaCaseError, match="JSON-serializable"):
        case.run(lambda c: {c.id})


def test_case_uninstrumented_async_run_paths() -> None:
    async def _run() -> str:
        case = kensa_case(id="async", input="hello")

        async def _agent(c):
            return {"seen": c.input}

        result = await case.run(_agent)
        return cast(dict[str, str], result)["seen"]

    assert asyncio.run(_run()) == "hello"


def test_kensa_trace_and_span_edge_paths() -> None:
    span = KensaSpan(name="s", start_time_unix_nano=None, end_time_unix_nano=None)
    assert span.duration_ms == 0
    assert KensaSpan(name="s", attributes={"cost_usd": "bad"}).cost_usd == 0
    llm_span = KensaSpan(name="llm", kind="llm", tool_name="lookup", attributes={"cost_usd": 0.2})
    trace = KensaTrace()
    assert trace.duration_ms == 0
    trace.replace([span, llm_span])
    assert trace.duration_ms == 0
    assert not hasattr(trace, "called")
    assert trace.tools.names == ["lookup"]
    assert trace.tools.include([])
    assert trace.tools.include(["lookup"])
    assert not trace.tools.include(["missing"])
    assert trace.tools.exclude(["missing"])
    assert trace.tools.order([])
    assert trace.tools.order(["lookup"])
    assert not trace.tools.order(["missing"])
    assert trace.tools.no_repeats()
    assert trace.cost_usd == 0.2
    assert trace.llm_turns == 1
    assert trace.to_dict()["llm_turns"] == 1


def test_record_llm_call_counts_toward_kensa_trace_llm_turns() -> None:
    from kensa import record_llm_call

    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test_record_llm_call",
        group_id="group",
        case_id="case",
        no_judge=False,
    )

    def kensa_run(case: KensaCase) -> dict[str, str]:
        with record_llm_call(provider="test-provider", model="test-model"):
            return {"seen": str(case.input)}

    assert runtime.run_case(kensa_case(id="llm_case", input="hello"), kensa_run) == {
        "seen": "hello"
    }
    assert runtime.trace.llm_turns == 1
    llm_spans = [span for span in runtime.trace.spans if span.kind == "llm"]
    assert len(llm_spans) == 1
    assert llm_spans[0].attributes["kensa.llm.provider"] == "test-provider"
    assert llm_spans[0].attributes["kensa.llm.model"] == "test-model"


def test_wait_status_renders_only_on_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str] | str] = []

    class FakeStatus:
        def __enter__(self) -> None:
            calls.append("enter")

        def __exit__(self, *args: object) -> None:
            calls.append("exit")

    class FakeTerminalConsole:
        is_terminal = True

        def status(self, text: str, *, spinner: str) -> FakeStatus:
            calls.append((text, spinner))
            return FakeStatus()

    monkeypatch.setattr(cli_output, "ERR_CONSOLE", FakeTerminalConsole())

    with cli_output.wait_status("Checking [status]"):
        calls.append("body")

    assert calls == [("Checking \\[status]", "line"), "enter", "body", "exit"]

    class FakeNonTerminalConsole:
        is_terminal = False

        def status(self, text: str, *, spinner: str) -> FakeStatus:
            raise AssertionError("status should not render off-terminal")

    calls.clear()
    monkeypatch.setattr(cli_output, "ERR_CONSOLE", FakeNonTerminalConsole())

    with cli_output.wait_status("Checking"):
        calls.append("body")

    assert calls == ["body"]


def test_judge_custom_provider_and_environment_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    class Provider:
        def judge(self, **kwargs: Any) -> JudgeResult:
            return JudgeResult(True, f"ok {kwargs['criteria']}", evidence=["e"])

    set_judge_provider(Provider())
    assert judge("out", "criteria").passed
    set_judge_provider(None)

    monkeypatch.setenv("KENSA_JUDGE_RESULT", "yes")
    assert judge("out", "criteria").model == "KENSA_JUDGE_RESULT"
    monkeypatch.delenv("KENSA_JUDGE_RESULT")
    with monkeypatch.context() as context:
        context.setattr("kensa.judge._provider_from_environment", lambda: None)
        assert judge("out", "criteria").error

    monkeypatch.delenv("KENSA_JUDGE_MODEL", raising=False)
    monkeypatch.delenv("KENSA_LLM_MODEL", raising=False)
    monkeypatch.delenv("KENSA_JUDGE_PROVIDER", raising=False)
    monkeypatch.delenv("KENSA_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def complete_with_evidence(*args: Any, **kwargs: Any) -> LLMResult:
        del args
        payload = {"passed": True, "reasoning": "ok", "evidence": ["single"]}
        return LLMResult(
            content='{"passed": true, "reasoning": "ok", "evidence": ["single"]}',
            provider=cast(str, kwargs["provider"]),
            model=cast(str, kwargs["model"]),
            parsed=payload,
        )

    monkeypatch.setattr("kensa.judge.complete", complete_with_evidence)
    default_judge = judge("out", "criteria")
    assert default_judge.model == DEFAULT_LLM_MODEL
    assert default_judge.provider == "openai"
    assert default_judge.evidence == ["single"]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test")
    anthropic_judge = judge("out", "criteria")
    assert anthropic_judge.model == "claude-sonnet-4-6"
    assert anthropic_judge.provider == "anthropic"
    monkeypatch.delenv("ANTHROPIC_API_KEY")

    monkeypatch.setenv("KENSA_JUDGE_PROVIDER", "anthropic")
    provider_judge = judge("out", "criteria")
    assert provider_judge.model == "claude-sonnet-4-6"
    assert provider_judge.provider == "anthropic"
    monkeypatch.setenv("KENSA_JUDGE_PROVIDER", "openai")
    openai_provider_judge = judge("out", "criteria")
    assert openai_provider_judge.model == DEFAULT_LLM_MODEL
    assert openai_provider_judge.provider == "openai"
    monkeypatch.delenv("KENSA_JUDGE_PROVIDER")

    monkeypatch.setenv("KENSA_JUDGE_MODEL", "gpt-5.5")

    def complete_without_evidence(*args: Any, **kwargs: Any) -> LLMResult:
        del args, kwargs
        return LLMResult(
            content='{"passed": false, "reasoning": "no"}',
            parsed={"passed": False, "reasoning": "no"},
        )

    monkeypatch.setattr("kensa.judge.complete", complete_without_evidence)
    fallback_judge = judge("out", "criteria")
    assert fallback_judge.evidence == []
    assert fallback_judge.provider == "openai"
    assert fallback_judge.model == "gpt-5.5"


def test_cli_edge_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == 2
    assert cli._latest_result_artifact(tmp_path / "missing") is None
    result_dir = tmp_path / ".kensa" / "results"
    result_dir.mkdir(parents=True)
    artifact = result_dir / "run.json"
    artifact.write_text(
        json.dumps(
            {
                "aggregates": [
                    {
                        "verdict": "pass",
                        "group_id": "g",
                        "case_id": "domain_case",
                        "passed": 1,
                        "total": 1,
                    }
                ]
            }
        )
    )
    cli._write_markdown_report(artifact, tmp_path / "report.md")
    assert "Kensa Eval Report" in (tmp_path / "report.md").read_text()
    artifact.write_text("{")
    assert cli._latest_eval_readiness().evals_ready is False
    skipped_readiness = cli._eval_readiness({"aggregates": [{}, {"verdict": "fail"}]})
    assert skipped_readiness.evals_ready is False
    artifact.write_text(
        json.dumps(
            {
                "aggregates": [
                    {
                        "verdict": "pass",
                        "group_id": "g",
                        "case_id": "domain_case",
                        "passed": 1,
                        "total": 1,
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    args = argparse.Namespace(paths=[], no_judge=True, json_report=None, markdown_report=None)
    assert cli._cmd_eval(args, ["-k", "x"]) == 0
    assert capsys.readouterr().err == ""
    assert cli._cmd_eval(args, []) == 0
    args = argparse.Namespace(
        paths=[],
        no_judge=False,
        json_report=str(tmp_path / "eval.json"),
        markdown_report=str(tmp_path / "eval.md"),
    )
    assert cli._cmd_eval(args, []) == 0
    assert (tmp_path / "eval.json").exists()
    assert (tmp_path / "eval.md").exists()

    readiness = cli._EvalReadiness(
        harness_smoke_count=0,
        domain_eval_count=0,
        trace_artifact=None,
    )
    cli._print_eval_readiness_terminal(readiness, ["custom warning"], [], [])
    assert "custom warning" in capsys.readouterr().out

    existing = tmp_path / "exists.txt"
    existing.write_text("old")
    cli._write_if_missing(existing, "new")
    assert existing.read_text() == "old"
    assert cli._write_text_if_changed(existing, "old") is False
    assert cli._find_git_root(Path("/")) == Path("/")
    assert cli._summarize_init_added_paths(
        [
            Path(".agents/skills/kensa-setup/SKILL.md"),
            Path(".agents/skills/kensa-setup/extra.md"),
        ]
    ) == [".agents/skills/kensa-setup/ (2 files)"]

    monkeypatch.setenv("LOCAL_URL", "http://localhost:3000")
    monkeypatch.setenv("LOCAL_DOMAIN_URL", "https://service.local")
    monkeypatch.setenv("BAD_URL", "not-url")
    assert "LOCAL_URL" not in cli._non_local_endpoint_markers()

    monkeypatch.setenv("PRODUCTION_URL", "https://prod.example.com")
    monkeypatch.setattr(
        cli,
        "_run_persistent_smoke",
        lambda: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )
    assert cli.main(["doctor"]) == 0
    monkeypatch.setattr(
        cli,
        "_run_persistent_smoke",
        lambda: subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom"),
    )
    assert cli.main(["doctor"]) == 1
    assert cli.main(["init"]) == 0

    monkeypatch.setattr(
        cli,
        "_run_persistent_smoke",
        lambda: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )
    monkeypatch.delenv("PRODUCTION_URL", raising=False)
    assert cli._run_doctor_check().returncode == 0
    trace_source = tmp_path / "traces.jsonl"
    trace_source.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "redaction": {
                    "version": "kensa.redactor.v2",
                    "mandatory": True,
                    "language": "en",
                    "value_redaction_applied": True,
                    "redaction_available": True,
                    "ruleset_hash": cli.redact.RULESET_HASH,
                    "pseudonymization": "instance-counter",
                    "model": {
                        "name": "en_core_web_sm",
                        "version": "3.8.0",
                        "checksum_verified": True,
                    },
                }
            }
        )
    )
    trace_source.write_text(
        json.dumps(
            {
                "schema_version": "kensa.trace_view.v1",
                "id": "tr",
                "name": None,
                "source": {
                    "provider": "jsonl",
                    "import_run_id": "import",
                    "imported_at": "2026-06-30T00:00:00Z",
                    "source_path": "traces.jsonl",
                    "source_url": None,
                    "trace_url": None,
                },
                "started_at_unix_nano": None,
                "ended_at_unix_nano": None,
                "duration_ms": 0.0,
                "status": "unknown",
                "input": None,
                "output": None,
                "attributes": {},
                "spans": [],
                "raw": None,
            }
        )
        + "\n"
    )
    assert (
        cli_traces.cmd_traces(
            argparse.Namespace(traces_command="sample", source=str(trace_source), json=False)
        )
        == 0
    )
    assert '"tr"' in capsys.readouterr().out
    assert (
        cli_traces.cmd_traces(
            argparse.Namespace(
                traces_command="get",
                source=str(trace_source),
                trace_id="missing",
                json=False,
            )
        )
        == 1
    )
    assert (
        cli_traces.cmd_traces(argparse.Namespace(traces_command="bad", source=str(trace_source)))
        == 2
    )
    assert (
        cli_traces.cmd_traces(
            argparse.Namespace(traces_command="bad", source=str(trace_source), json=True)
        )
        == 2
    )
    empty_source = tmp_path / "empty.jsonl"
    empty_source.write_text("")
    manifest_text = trace_source.with_suffix(".manifest.json").read_text()
    empty_source.with_suffix(".manifest.json").write_text(manifest_text)
    assert (
        cli_traces.cmd_traces(
            argparse.Namespace(traces_command="sample", source=str(empty_source), json=False)
        )
        == 0
    )
    assert (
        cli_traces.cmd_traces(
            argparse.Namespace(traces_command="sample", source=str(empty_source), json=True)
        )
        == 0
    )
    assert (
        cli_traces.cmd_traces(
            argparse.Namespace(traces_command="list", source="local-dev", json=False)
        )
        == 1
    )
    monkeypatch.setenv("KENSA_ENABLE_LOCAL_DEV_TRACES", "1")
    monkeypatch.delenv("KENSA_LOCAL_DEV_TRACES", raising=False)
    assert (
        cli_traces.cmd_traces(
            argparse.Namespace(traces_command="list", source="local-dev", json=False)
        )
        == 1
    )
    assert (
        cli_traces.cmd_traces(
            argparse.Namespace(traces_command="list", source="local-dev", json=True)
        )
        == 1
    )

    monkeypatch.setattr(
        cli_traces,
        "load_trace_views",
        lambda source, **kwargs: (_ for _ in ()).throw(ValueError("bad source")),
    )
    assert (
        cli_traces.cmd_traces(argparse.Namespace(traces_command="list", source="bad", json=False))
        == 1
    )


def test_pytest_plugin_direct_helpers() -> None:
    assert (
        pytest_make_parametrize_id(cast(Any, None), kensa_case(id="case_id", input="x"), "case")
        == "case_id"
    )
    assert pytest_make_parametrize_id(cast(Any, None), KensaTrial(2, 3), "_kensa_trial") == "trial2"
    assert pytest_make_parametrize_id(cast(Any, None), object(), "x") is None
    trial_fixture = cast(Any, _kensa_trial_fixture).__wrapped__
    trace_fixture = cast(Any, kensa_trace_fixture).__wrapped__
    assert trial_fixture(SimpleNamespace(param="bad")).id == "trial1"
    assert trial_fixture(SimpleNamespace(param=KensaTrial(3, 3))).id == "trial3"
    assert isinstance(trace_fixture(SimpleNamespace(node=object())), KensaTrace)
    assert _marker_trials(cast(Any, SimpleNamespace(args=[2], kwargs={}))) == 2
    with pytest.raises(pytest.UsageError):
        _marker_trials(cast(Any, SimpleNamespace(args=["bad"], kwargs={})))
    with pytest.raises(pytest.UsageError):
        _marker_trials(cast(Any, SimpleNamespace(args=[], kwargs={"trials": 0})))
    aggregate = KensaAggregate(
        group_id="g",
        case_id="c",
        configured_trials=1,
        total=1,
        passed=1,
        failed=0,
        errored=0,
        partial=False,
        verdict="pass",
        trials=[],
    )
    assert aggregate.to_dict()["verdict"] == "pass"
    assert _case_id(cast(Any, SimpleNamespace())) == "default"
    markerless_item = SimpleNamespace(
        callspec=SimpleNamespace(params={PRIVATE_TRIAL: KensaTrial(1, 1)}),
        get_closest_marker=lambda name: None,
    )
    assert _runtime_for_item(cast(Any, markerless_item)) is None
    config = SimpleNamespace(getoption=lambda name: None)
    state = KensaSessionState(cast(Any, config))
    assert state.artifact_dir == Path.cwd() / ".kensa"
    assert not state.write_artifacts

    class Terminal:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def write_sep(self, sep: str, title: str) -> None:
            self.lines.append(f"{sep}{title}")

        def write_line(self, line: str) -> None:
            self.lines.append(line)

    class Config:
        def getoption(self, name: str) -> Any:
            return "json" if name == "--kensa-report" else None

    term = Terminal()
    config_obj = Config()
    state = KensaSessionState(cast(Any, config_obj))
    state.aggregates = [aggregate]
    state.trials = [
        TrialMetadata(
            nodeid="n",
            group_id="g",
            case_id="c",
            trial_index=1,
            configured_trials=1,
            status="pass",
        )
    ]
    config_obj.__dict__["_kensa_state"] = state
    pytest_terminal_summary(cast(Any, term), 0, cast(Any, config_obj))
    assert any('"aggregates"' in line for line in term.lines)

    class Outcome:
        def __init__(self, report: Any) -> None:
            self._report = report

        def get_result(self) -> Any:
            return self._report

    non_runtime_item = SimpleNamespace(nodeid="n", config=config_obj)
    report = SimpleNamespace(when="setup", failed=True)
    call = SimpleNamespace(excinfo=None)
    hook = pytest_runtest_makereport(cast(Any, non_runtime_item), cast(Any, call))
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(Outcome(report))

    runtime_item = SimpleNamespace(
        nodeid="n[trial1]",
        config=config_obj,
        callspec=SimpleNamespace(params={PRIVATE_TRIAL: KensaTrial(1, 1)}),
        get_closest_marker=lambda name: (
            SimpleNamespace(
                args=[],
                kwargs={},
            )
            if name == "kensa"
            else None
        ),
    )
    state.trials = [
        TrialMetadata(
            nodeid="n[trial1]",
            group_id="n",
            case_id="default",
            trial_index=1,
            configured_trials=1,
            status="error",
        )
    ]
    hook = pytest_runtest_makereport(cast(Any, runtime_item), cast(Any, call))
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(Outcome(report))


def test_runtime_direct_error_and_flush_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    case = kensa_case(id="runtime", input="hello")
    runtime = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="test.py::test_runtime[trial1-runtime]",
        group_id="g",
        case_id="runtime",
        no_judge=False,
    )
    token = set_current_runtime(runtime)
    assert current_runtime() is runtime
    reset_current_runtime(token)
    assert current_runtime() is None

    with pytest.raises(RuntimeError, match="sync"):
        runtime.run_case(case, lambda c: (_ for _ in ()).throw(RuntimeError("sync")))
    runtime2 = KensaTrialRuntime(
        trial=KensaTrial(1, 1),
        nodeid="n",
        group_id="g",
        case_id="runtime",
        no_judge=False,
    )

    async def _bad(c):
        raise RuntimeError("async")

    with pytest.raises(RuntimeError, match="async"):
        asyncio.run(runtime2.run_case(case, _bad))

    class TypeErrorFlush:
        def force_flush(self) -> bool:
            return False

    runtime2._trace_id = "missing"
    monkeypatch.setattr("kensa.runtime.trace.get_tracer_provider", lambda: TypeErrorFlush())
    runtime2._flush_and_populate_trace()
    assert runtime2.trace.incomplete

    class ExceptionFlush:
        def force_flush(self, timeout_millis: int | None = None) -> bool:
            raise RuntimeError("flush failed")

    monkeypatch.setattr("kensa.runtime.trace.get_tracer_provider", lambda: ExceptionFlush())
    runtime2._flush_and_populate_trace()
    assert "flush failed" in str(runtime2.trace.incomplete_reason)
    assert collect_spans(None) == []
    assert KensaSpan(name="x", attributes={"bad": {1, 2}}).to_dict()["attributes"]["bad"] == {1, 2}
    with pytest.raises(KensaCaseError, match="JSON-serializable"):
        runtime2._record_output_and_trace({1})

    class DuplicateExporter:
        def get_finished_spans(self) -> list[Any]:
            context = SimpleNamespace(trace_id=1, span_id=2)
            raw = SimpleNamespace(
                name="dup",
                parent=None,
                start_time=None,
                end_time=None,
                attributes={},
                status=SimpleNamespace(status_code=SimpleNamespace(name="OK")),
                get_span_context=lambda: context,
            )
            return [raw, raw]

    import kensa.runtime as runtime_module

    previous_exporter = runtime_module._EXPORTER
    runtime_module._EXPORTER = DuplicateExporter()
    assert len(collect_spans("00000000000000000000000000000001")) == 1
    runtime_module._EXPORTER = previous_exporter
    assert runtime_module.jsonable({1, 2}).startswith("{")

    monkeypatch.setattr(
        "kensa.runtime.trace.set_tracer_provider",
        lambda provider: (_ for _ in ()).throw(RuntimeError("set")),
    )
    monkeypatch.setattr(
        "kensa.runtime.trace.get_tracer_provider",
        lambda: SimpleNamespace(_kensa_exporter="fallback"),
    )

    previous_ready = runtime_module._PROVIDER_READY
    previous_exporter = runtime_module._EXPORTER
    runtime_module._PROVIDER_READY = False
    ensure_tracing()
    assert runtime_module._EXPORTER == "fallback"
    runtime_module._PROVIDER_READY = previous_ready
    runtime_module._EXPORTER = previous_exporter


def test_tracing_exporter_edge_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = JSONLSpanExporter(tmp_path / "spans.jsonl")
    assert exporter.force_flush() is True
    assert exporter.shutdown() is None
    manifest_exporter = JSONLSpanExporter(tmp_path / "run" / "spans.jsonl", run_id="run")
    assert manifest_exporter.force_flush() is True
    assert manifest_exporter.shutdown() is None
    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text())
    assert manifest["span_count"] == 0
    monkeypatch.delenv("KENSA_TRACE_DIR", raising=False)
    instrument()

    class Provider:
        pass

    from kensa import tracing

    assert not tracing._add_jsonl_processor(Provider(), tmp_path / "x.jsonl")
    assert tracing.jsonable({1, 2}).startswith("{")

    class NoContextSpan:
        name = "no_context"
        parent = None
        start_time = None
        end_time = None
        status = SimpleNamespace(status_code=SimpleNamespace(name="OK"))

        def __init__(self) -> None:
            self.attributes: dict[str, Any] = {}

        def get_span_context(self) -> None:
            return None

    assert tracing.span_to_dict(cast(Any, NoContextSpan()))["trace_id"] is None

    class ProviderWithProcessor:
        def __init__(self) -> None:
            self.processors: list[Any] = []

        def add_span_processor(self, processor: Any) -> None:
            self.processors.append(processor)

    provider = ProviderWithProcessor()
    monkeypatch.setattr("kensa.tracing.trace.get_tracer_provider", lambda: provider)
    instrument(tmp_path / "instrument")
    assert provider.processors
    provider_path = ProviderWithProcessor()
    assert tracing._add_jsonl_processor(provider_path, tmp_path / "path.jsonl")
    assert provider_path.processors

    class LinkContextSpan:
        name = "with_link"
        parent = None
        start_time = None
        end_time = None
        status = SimpleNamespace(status_code=SimpleNamespace(name="OK"))

        def __init__(self) -> None:
            self.attributes: dict[str, Any] = {}
            self.events: list[Any] = []
            self.links = [
                SimpleNamespace(
                    context=SimpleNamespace(trace_id=1, span_id=2),
                    attributes={"link_attr": {1, 2}},
                )
            ]

        def get_span_context(self) -> Any:
            return SimpleNamespace(trace_id=3, span_id=4, trace_state="state")

    link_row = tracing.span_to_dict(cast(Any, LinkContextSpan()))
    assert link_row["links"][0]["span_id"] == "0000000000000002"
    assert link_row["links"][0]["attributes"]["link_attr"].startswith("{")

    class NoAddProvider:
        pass

    monkeypatch.setattr("kensa.tracing.trace.get_tracer_provider", lambda: NoAddProvider())
    monkeypatch.setattr(
        "kensa.tracing.trace.set_tracer_provider",
        lambda provider: (_ for _ in ()).throw(RuntimeError("set")),
    )
    instrument(tmp_path / "fallback")


def test_cli_module_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "kensa.cli", raising=False)
    monkeypatch.setattr("sys.argv", ["kensa", "--help"])
    cli_path = Path(__file__).resolve().parents[1] / "src" / "kensa" / "cli.py"
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(cli_path), run_name="__main__")
    assert excinfo.value.code == 0
