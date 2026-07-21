from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from kensa import pytest_plugin
from kensa.case import KensaCaseError, KensaMessage, kensa_case
from kensa.runtime import KensaTrial
from kensa.watchdog import (
    WatchdogControl,
    read_control,
    write_control,
)


def _run_kensa_xdist(
    pytester: pytest.Pytester,
    *args: str,
    workers: int = 2,
) -> Any:
    root = Path(str(pytester.path))
    control_path = root / ".kensa" / "state" / "test-run.json"
    write_control(
        control_path,
        WatchdogControl(
            run_id="test-run",
            result_path=root / ".kensa" / "results" / "test-run.json",
            artifact_dir=root / ".kensa",
            default_timeout_s=30,
            expected_workers=workers,
        ),
    )
    return pytester.runpytest(
        *args,
        "--kensa-control-path",
        str(control_path),
    )


def _tool_call(**overrides: Any) -> dict[str, Any]:
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }
    tool_call.update(overrides)
    return tool_call


def _assistant_tool_message(*tool_calls: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, "tool_calls": list(tool_calls)}


def test_kensa_case_requires_id() -> None:
    with pytest.raises(KensaCaseError):
        kensa_case(id="", input="hello")


def test_kensa_case_is_public_immutable_data() -> None:
    case = kensa_case(id="c1", messages=[{"role": "user", "content": "hello"}], customer="a")

    assert case.id == "c1"
    assert case.input == [{"role": "user", "content": "hello"}]
    assert case.messages[-1]["content"] == "hello"
    assert case.row["customer"] == "a"
    with pytest.raises(TypeError):
        cast(Any, case.row)["customer"] = "b"


def test_kensa_case_accepts_developer_system_and_tool_role_messages() -> None:
    messages: list[KensaMessage] = [
        {"role": "developer", "content": "Prefer refunds only after verifying orders."},
        {"role": "system", "content": "Follow refund policy."},
        {"role": "user", "content": "Refund my last charge."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_lookup_customer",
                    "type": "function",
                    "function": {
                        "name": "lookup_customer",
                        "arguments": '{"customer_id": "cus_123"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_lookup_customer",
            "content": '{"customer_id": "cus_123", "order_history": []}',
        },
        {"role": "assistant", "content": "I found the account, but no order history."},
    ]

    case = kensa_case(id="c1", messages=messages)

    assert case.input == messages
    assert case.messages == messages
    assert case.messages[0]["role"] == "developer"
    assert case.messages[1]["role"] == "system"
    assert case.messages[3]["role"] == "assistant"
    assert case.messages[4]["role"] == "tool"
    assert case.messages[4]["tool_call_id"] == "call_lookup_customer"


def test_kensa_case_keeps_input_lists_as_unvalidated_payloads() -> None:
    raw_input = [{"role": "assistant", "content": None}]
    case = kensa_case(id="raw_input", input=raw_input)

    assert case.input == raw_input
    with pytest.raises(KensaCaseError, match=r"messages=\.\.\. was provided"):
        _ = case.messages


@pytest.mark.parametrize(
    ("messages", "match"),
    [
        ([], "at least one message"),
        ([{"role": "admin", "content": "hello"}], "role must be"),
        ([{"role": "user", "content": 1}], "content must be a string"),
        ([{"role": "user", "content": "hello", "extra": True}], "unsupported keys"),
        ([{"role": "user", "content": "hello", "name": 1}], "name must be a string"),
        ([{"role": "assistant"}], "require string content"),
        ([{"role": "assistant", "content": None}], "require string content"),
        ([{"role": "assistant", "content": []}], "require string content"),
        ([{"role": "assistant", "content": None, "tool_calls": []}], "non-empty list"),
        ([{"role": "assistant", "content": None, "tool_calls": ["bad"]}], "must be an object"),
        (
            [{"role": "assistant", "content": [], "tool_calls": [_tool_call()]}],
            "content must be a string or None",
        ),
        ([_assistant_tool_message(_tool_call(id=""))], "id must be a non-empty string"),
        ([_assistant_tool_message(_tool_call(), _tool_call())], "duplicate assistant tool_call id"),
        ([_assistant_tool_message(_tool_call(type="custom"))], "type='function'"),
        ([_assistant_tool_message(_tool_call(index=0))], r"tool_calls\[0\].*unsupported keys"),
        ([_assistant_tool_message(_tool_call(function="lookup"))], "function must be an object"),
        ([_assistant_tool_message(_tool_call(function={"name": "", "arguments": "{}"}))], "name"),
        (
            [
                _assistant_tool_message(
                    _tool_call(function={"name": "lookup", "arguments": "{}", "extra": True})
                )
            ],
            r"function.*unsupported keys",
        ),
        (
            [_assistant_tool_message(_tool_call(function={"name": "lookup", "arguments": {}}))],
            "JSON object string",
        ),
        (
            [_assistant_tool_message(_tool_call(function={"name": "lookup", "arguments": "{"}))],
            "valid JSON",
        ),
        (
            [_assistant_tool_message(_tool_call(function={"name": "lookup", "arguments": "[]"}))],
            "JSON object string",
        ),
        ([{"role": "tool", "tool_call_id": "", "content": "{}"}], "non-empty tool_call_id"),
        ([{"role": "tool", "tool_call_id": "call_1", "content": "{}"}], "unknown tool_call_id"),
        (
            [
                _assistant_tool_message(_tool_call()),
                {"role": "tool", "tool_call_id": "call_1", "content": {}},
            ],
            "tool message content must be a string",
        ),
        (
            [_assistant_tool_message(_tool_call()), {"role": "user", "content": "next"}],
            "must be followed",
        ),
        ([_assistant_tool_message(_tool_call())], "must be followed"),
    ],
)
def test_kensa_case_rejects_invalid_message_contracts(
    messages: list[dict[str, Any]], match: str
) -> None:
    with pytest.raises(KensaCaseError, match=match):
        kensa_case(id="bad", messages=cast(Any, messages))


def test_one_case_three_trials_collects_three_items(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=3)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case):
    assert case.input == "hello"
"""
    )

    result = pytester.runpytest("--collect-only", "-q")

    result.stdout.fnmatch_lines(
        [
            "*test_agent[trial1-case_a]*",
            "*test_agent[trial2-case_a]*",
            "*test_agent[trial3-case_a]*",
        ]
    )
    assert result.ret == 0


def test_explicit_timeout_requires_kensa_eval(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_eval="""
import pytest


@pytest.mark.kensa(timeout_s=1)
def test_agent():
    pass
"""
    )

    result = pytester.runpytest("-q")

    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.stdout.fnmatch_lines(["*timeout_s=...*requires kensa eval for hard containment*"])


def test_kensa_marker_without_type_runs_successfully(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case):
    assert case.input == "hello"
"""
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=1)


def test_two_cases_three_trials_collects_six_items(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=3)
@pytest.mark.parametrize("case", [
    kensa_case(id="case_a", input="a"),
    kensa_case(id="case_b", input="b"),
])
def test_agent(case):
    assert case.input in {"a", "b"}
"""
    )

    result = pytester.runpytest("--collect-only", "-q")

    assert result.ret == 0
    assert "6 tests collected" in result.stdout.str()


def test_trials_compose_with_user_parametrize_without_hidden_argument(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=2)
@pytest.mark.parametrize("mode", ["fast", "slow"])
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, mode):
    assert mode in {"fast", "slow"}
    assert case.input == "hello"
"""
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=4)
    result.stdout.fnmatch_lines(["*2/2 aggregate case(s) passed*"])
    summary_rows = [line for line in result.stdout.lines if line.startswith("✓ pass")]
    assert len(summary_rows) == 2
    assert any("test_agent[case_a-fast]" in line for line in summary_rows)
    assert any("test_agent[case_a-slow]" in line for line in summary_rows)
    assert all("✓ T1  ✓ T2" in line for line in summary_rows)


def test_plain_failing_assertions_fail_trials_and_aggregate_fail(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=2)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case):
    assert False
"""
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(failed=2)
    assert result.ret == 1
    result.stdout.fnmatch_lines(["*✗ fail*case_a*✗ T1*✗ T2*"])


def test_mixed_trial_outcomes_are_flaky_and_fail_session(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case

count = 0


@pytest.mark.kensa(trials=3)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case):
    global count
    count += 1
    assert count != 2
"""
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=2, failed=1)
    assert result.ret == 1
    result.stdout.fnmatch_lines(["*! flaky*case_a*✓ T1*✗ T2*✓ T3*"])


def test_pytest_x_reports_partial_aggregate(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=3)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case):
    assert False
"""
    )

    result = pytester.runpytest("-q", "-x")

    result.assert_outcomes(failed=1)
    assert result.ret == 1
    result.stdout.fnmatch_lines(["*! partial*case_a*✗ T1*"])


def test_xdist_x_reports_unrun_trials_as_partial(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=3)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case):
    assert False
"""
    )

    result = _run_kensa_xdist(
        pytester,
        "-q",
        "-n",
        "2",
        "-x",
        "--kensa-write-artifacts",
    )

    assert result.ret == 1
    result.stdout.fnmatch_lines(["*! partial*case_a*✗ T1*"])
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    aggregate = payload["aggregates"][0]
    assert aggregate["verdict"] == "partial"
    assert aggregate["partial"] is True
    assert aggregate["total"] == aggregate["failed"]
    assert aggregate["total"] < aggregate["configured_trials"]
    assert aggregate["errored"] == 0
    assert len(aggregate["trials"]) == aggregate["total"]
    assert payload["complete"] is False
    assert payload["interruption"]["kind"] == "pytest_stopped"


def test_setup_errors_aggregate_as_error(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.fixture
def broken_fixture():
    raise RuntimeError("setup exploded")


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, broken_fixture):
    assert broken_fixture
"""
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(errors=1)
    assert result.ret == 1
    result.stdout.fnmatch_lines(["*ERROR *test_agent*"])


def test_teardown_errors_replace_pass_metadata_and_aggregate_error(
    pytester: pytest.Pytester,
) -> None:
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


@pytest.fixture
def bad_teardown():
    yield
    raise RuntimeError("teardown exploded")
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run, bad_teardown):
    assert case.run(kensa_run).output == {"ok": True}
"""
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(passed=1, errors=1)
    assert result.ret == 1
    result.stdout.fnmatch_lines(["*ERROR *test_agent*"])
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    assert payload["trials"][0]["status"] == "error"
    assert payload["trials"][0]["error_kind"] == "teardown"


def test_case_run_records_output_artifact(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import pytest
from kensa.pytest import ConversationResponse


@pytest.fixture
def kensa_run(case):
    class Agent:
        def respond(self, messages):
            return ConversationResponse(output={"input": case.input})
    return Agent()
"""
    )
    pytester.makepyfile(
        test_eval="""
import json
from pathlib import Path

import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run):
    output = case.run(kensa_run)
    assert output.output["input"] == "hello"
    artifact = next(Path(".kensa/results").glob("*.json"))
    snapshot = json.loads(artifact.read_text())
    assert snapshot["trials"][0]["status"] == "provisional"
"""
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(passed=1)
    artifacts = list((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text())
    assert payload["trials"][0]["status"] == "pass"
    assert payload["trials"][0]["output"]["output"] == {"input": "hello"}
    assert "type" not in payload["trials"][0]
    assert "type" not in payload["aggregates"][0]
    trace_artifact = next(
        (Path(str(pytester.path)) / ".kensa" / "traces" / "runs").glob("*/trials.jsonl")
    )
    trace_row = json.loads(trace_artifact.read_text().splitlines()[0])
    assert "type" not in trace_row


def test_first_response_failure_artifact_retains_initial_conversation(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(
        """
import pytest


@pytest.fixture
def kensa_run():
    class Agent:
        def respond(self, messages):
            raise RuntimeError("first response failed")
    return Agent()
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize(
    "case",
    [kensa_case(messages=[
        {"role": "system", "content": "private"},
        {"role": "user", "content": "initial"},
    ], id="case_a")],
)
def test_agent(case, kensa_run):
    case.run(kensa_run)
"""
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(failed=1)
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    assert payload["trials"][0]["output"] == {
        "messages": [
            {"role": "system", "content": "private"},
            {"role": "user", "content": "initial"},
        ],
        "output": None,
        "termination": None,
    }


def test_setup_error_replaces_provisional_case_snapshot(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import pytest
from kensa.pytest import ConversationResponse


@pytest.fixture
def kensa_run(case):
    class Agent:
        def respond(self, messages):
            return ConversationResponse(output={"input": case.input})
    return Agent()


@pytest.fixture
def failing_setup(case, kensa_run):
    case.run(kensa_run)
    raise RuntimeError("setup failed after case run")
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, failing_setup):
    raise AssertionError("test call should not run")
""",
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(errors=1)
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    trial = json.loads(artifact.read_text())["trials"][0]
    assert trial["status"] == "error"
    assert trial["error_kind"] == "setup"
    assert trial["error"] == "setup failed after case run"
    assert trial["case"] == {"id": "case_a", "input": "hello"}
    assert trial["output"]["output"] == {"input": "hello"}


def test_teardown_judge_preserves_finalized_trial_outcomes(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KENSA_JUDGE_RESULT", "pass")
    pytester.makeconftest(
        """
import json
from pathlib import Path

import pytest
from kensa.pytest import ConversationResponse, judge


@pytest.fixture
def kensa_run(case):
    class Agent:
        def respond(self, messages):
            return ConversationResponse(output={"input": case.input})
    return Agent()


@pytest.fixture
def judge_in_teardown(request):
    yield
    artifact = next(Path(".kensa/results").glob("*.json"))
    before = next(
        trial
        for trial in json.loads(artifact.read_text())["trials"]
        if trial["nodeid"] == request.node.nodeid
    )
    result = judge(before["output"], "teardown evidence must pass")
    assert result.passed
    after = next(
        trial
        for trial in json.loads(artifact.read_text())["trials"]
        if trial["nodeid"] == request.node.nodeid
    )
    for field in ("status", "duration_ms", "error", "error_kind"):
        assert after[field] == before[field]
    assert len(after["judges"]) == len(before["judges"]) + 1
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="pass_case", input="hello")])
def test_pass(case, kensa_run, judge_in_teardown):
    assert case.run(kensa_run).output == {"input": "hello"}


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="fail_case", input="hello")])
def test_fail(case, kensa_run, judge_in_teardown):
    case.run(kensa_run)
    assert False, "call failed"
""",
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(passed=1, failed=1)
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    trials = json.loads(artifact.read_text())["trials"]
    passed = next(trial for trial in trials if trial["case_id"] == "pass_case")
    failed = next(trial for trial in trials if trial["case_id"] == "fail_case")
    assert passed["status"] == "pass"
    assert len(passed["judges"]) == 1
    assert failed["status"] == "fail"
    assert failed["error"] == "call failed\nassert False"
    assert failed["error_kind"] == "assertion"
    assert len(failed["judges"]) == 1


def test_xdist_transports_successful_teardown_judge_metadata(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KENSA_JUDGE_RESULT", "pass")
    pytester.makeconftest(
        """
import pytest
from kensa.pytest import ConversationResponse, judge


@pytest.fixture
def kensa_run(case):
    class Agent:
        def respond(self, messages):
            return ConversationResponse(output={"input": case.input})
    return Agent()


@pytest.fixture
def judge_in_teardown():
    yield
    result = judge({"input": "hello"}, "teardown evidence must pass")
    assert result.passed
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="pass_case", input="hello")])
def test_pass(case, kensa_run, judge_in_teardown):
    assert case.run(kensa_run).output == {"input": "hello"}
""",
    )

    result = _run_kensa_xdist(
        pytester,
        "-q",
        "-n",
        "2",
        "--dist=load",
        "--kensa-write-artifacts",
    )

    result.assert_outcomes(passed=1)
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    trial = json.loads(artifact.read_text())["trials"][0]
    assert trial["status"] == "pass"
    assert len(trial["judges"]) == 1


def test_failed_assertions_still_write_trial_metadata(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import pytest
from kensa.pytest import ConversationResponse


@pytest.fixture
def kensa_run(case):
    class Agent:
        def respond(self, messages):
            return ConversationResponse(output={"input": case.input})
    return Agent()
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run):
    assert case.run(kensa_run).output == {"input": "hello"}
    assert False, "regression still present"
"""
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(failed=1)
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    assert payload["trials"][0]["status"] == "fail"
    assert payload["trials"][0]["output"]["output"] == {"input": "hello"}
    assert payload["trials"][0]["error_kind"] == "assertion"


def test_case_run_rejects_second_call(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import pytest
from kensa.pytest import ConversationResponse


@pytest.fixture
def kensa_run():
    class Agent:
        def respond(self, messages):
            return ConversationResponse(output="ok")
    return Agent()
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run):
    case.run(kensa_run)
    case.run(kensa_run)
"""
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*case.run(...) may be called at most once per trial*"])


def test_async_kensa_run_is_supported(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import pytest
from kensa.pytest import ConversationResponse


@pytest.fixture
def kensa_run(case):
    class Agent:
        async def respond(self, messages):
            return ConversationResponse(output={"value": case.input})
    return Agent()
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.asyncio
@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
async def test_agent(case, kensa_run):
    output = await case.run(kensa_run)
    assert output.output == {"value": "hello"}
"""
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=1)


def test_xdist_merges_trials_into_one_controller_artifact(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import pytest
from kensa.pytest import ConversationResponse


@pytest.fixture
def kensa_run(case):
    class Agent:
        def respond(self, messages):
            return ConversationResponse(output={"input": case.input})
    return Agent()
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=3)
@pytest.mark.parametrize("case", [
    kensa_case(id="case_a", input="a"),
    kensa_case(id="case_b", input="b"),
])
def test_agent(case, kensa_run):
    assert case.run(kensa_run).output == {"input": case.input}
"""
    )

    result = _run_kensa_xdist(
        pytester,
        "-q",
        "-n",
        "2",
        "--dist=load",
        "--kensa-write-artifacts",
    )

    result.assert_outcomes(passed=6)
    assert result.ret == 0
    assert result.stdout.str().count("Kensa evaluation complete") == 1
    result_dir = Path(str(pytester.path)) / ".kensa" / "results"
    artifacts = list(result_dir.glob("*.json"))
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text())
    assert payload["complete"] is True
    assert payload["interruption"] is None
    assert [(trial["case_id"], trial["trial_index"]) for trial in payload["trials"]] == [
        ("case_a", 1),
        ("case_a", 2),
        ("case_a", 3),
        ("case_b", 1),
        ("case_b", 2),
        ("case_b", 3),
    ]
    assert [aggregate["verdict"] for aggregate in payload["aggregates"]] == ["pass", "pass"]
    assert [aggregate["total"] for aggregate in payload["aggregates"]] == [3, 3]
    trace_files = list(
        (Path(str(pytester.path)) / ".kensa" / "traces" / "runs").glob("*/trials.jsonl")
    )
    assert len(trace_files) == 1
    trace_rows = [json.loads(line) for line in trace_files[0].read_text().splitlines()]
    assert len(trace_rows) == 6
    assert {row["case_id"] for row in trace_rows} == {"case_a", "case_b"}
    assert trace_files[0].parent.name == payload["run_id"]


def test_xdist_rejects_each_distribution_for_kensa_trials(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import ConversationResponse
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case):
    assert case.input == "hello"
"""
    )

    result = _run_kensa_xdist(
        pytester,
        "-q",
        "-n",
        "2",
        "--dist=each",
        "--kensa-write-artifacts",
    )

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "--dist=each is incompatible with Kensa trials" in result.stderr.str()
    assert not list((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))


def test_xdist_each_distribution_runs_plain_pytest_suite(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_plain="""
def test_plain():
    assert True
"""
    )

    result = pytester.runpytest("-q", "-n", "2", "--dist=each")

    result.assert_outcomes(passed=2)
    assert result.ret == 0


def test_direct_xdist_kensa_run_is_not_watchdog_managed(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case):
    assert case.input == "hello"
"""
    )

    result = pytester.runpytest("-q", "-n", "2")

    result.assert_outcomes(passed=1)
    assert result.ret == 0


def test_xdist_merges_flaky_trial_outcome(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=3)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, request):
    assert "trial2" not in request.node.nodeid
"""
    )

    result = _run_kensa_xdist(
        pytester,
        "-q",
        "-n",
        "2",
        "--kensa-write-artifacts",
    )

    result.assert_outcomes(passed=2, failed=1)
    assert result.ret == 1
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    assert len(payload["aggregates"]) == 1
    assert payload["aggregates"][0]["verdict"] == "flaky"
    assert [trial["trial_index"] for trial in payload["trials"]] == [1, 2, 3]


def test_xdist_transports_setup_and_teardown_error_metadata(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(
        """
import pytest
from kensa.pytest import ConversationResponse


@pytest.fixture
def kensa_run(case):
    class Agent:
        def respond(self, messages):
            return ConversationResponse(output={"input": case.input})
    return Agent()


@pytest.fixture
def conditional_lifecycle(request):
    if "case_c" in request.node.nodeid:
        raise RuntimeError("setup exploded")
    yield
    if "case_b" in request.node.nodeid:
        raise RuntimeError("teardown exploded")
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [
    kensa_case(id="case_a", input="a"),
    kensa_case(id="case_b", input="b"),
    kensa_case(id="case_c", input="c"),
])
def test_agent(case, kensa_run, conditional_lifecycle):
    assert case.run(kensa_run).output == {"input": case.input}
"""
    )

    result = _run_kensa_xdist(
        pytester,
        "-q",
        "-n",
        "2",
        "--kensa-write-artifacts",
    )

    result.assert_outcomes(passed=2, errors=2)
    assert result.ret == 1
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    trials = {trial["case_id"]: trial for trial in payload["trials"]}
    assert trials["case_a"]["status"] == "pass"
    assert trials["case_b"]["status"] == "error"
    assert trials["case_b"]["error_kind"] == "teardown"
    assert trials["case_c"]["status"] == "error"
    assert trials["case_c"]["error_kind"] == "setup"
    aggregates = {aggregate["case_id"]: aggregate for aggregate in payload["aggregates"]}
    assert aggregates["case_a"]["verdict"] == "pass"
    assert aggregates["case_b"]["verdict"] == "error"
    assert aggregates["case_c"]["verdict"] == "error"


def test_xdist_marks_teardown_worker_crash_incomplete(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(
        """
import os
import pytest
from kensa.pytest import ConversationResponse


@pytest.fixture
def kensa_run(case):
    class Agent:
        def respond(self, messages):
            return ConversationResponse(output={"input": case.input})
    return Agent()


@pytest.fixture
def crash_in_teardown():
    yield
    if os.environ.get("PYTEST_XDIST_WORKER"):
        os._exit(3)
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run, crash_in_teardown):
    assert case.run(kensa_run).output == {"input": "hello"}
"""
    )

    result = _run_kensa_xdist(
        pytester,
        "-q",
        "-n",
        "2",
        "--kensa-write-artifacts",
    )

    assert result.ret == 1
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    assert len(payload["trials"]) == 1
    assert payload["trials"][0]["status"] == "pass"
    assert payload["trials"][0]["error_kind"] is None
    assert payload["aggregates"][0]["verdict"] == "pass"
    assert payload["aggregates"][0]["partial"] is False
    assert payload["complete"] is False
    assert payload["interruption"]["kind"] == "worker_crash"


def test_xdist_marks_setup_worker_crash_incomplete_without_synthetic_trial(
    pytester: pytest.Pytester,
) -> None:
    kensa_dir = Path(str(pytester.path)) / "kensa_suite"
    sibling_dir = Path(str(pytester.path)) / "sibling_suite"
    kensa_dir.mkdir()
    sibling_dir.mkdir()
    (kensa_dir / "conftest.py").write_text(
        """
import os
import pytest

_started = set()
_setup_reports = {}


def pytest_runtest_logstart(nodeid, location):
    del location
    _started.add(nodeid)


def pytest_runtest_logreport(report):
    assert report.nodeid in _started, "report arrived before logstart"
    if report.when == "setup":
        _setup_reports[report.nodeid] = _setup_reports.get(report.nodeid, 0) + 1
        assert _setup_reports[report.nodeid] == 1, "duplicate setup report"


@pytest.fixture
def crash_in_setup():
    if os.environ.get("PYTEST_XDIST_WORKER"):
        os._exit(3)
"""
    )
    (kensa_dir / "test_eval.py").write_text(
        """
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, crash_in_setup):
    assert case.input == "hello"
"""
    )
    (sibling_dir / "conftest.py").write_text(
        """
def pytest_runtest_logreport(report):
    if report.when == "setup" and report.passed:
        assert "kensa_suite" not in report.nodeid, "received sibling setup report"
"""
    )
    (sibling_dir / "test_plain.py").write_text(
        """
def test_plain():
    assert True
"""
    )

    result = _run_kensa_xdist(
        pytester,
        "-q",
        "-n",
        "2",
        "--kensa-write-artifacts",
        str(kensa_dir),
        str(sibling_dir),
    )

    assert result.ret == 1
    assert "INTERNALERROR" not in result.stdout.str()
    result.assert_outcomes(passed=1, failed=1)
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    assert payload["trials"] == []
    assert payload["aggregates"] == []
    assert payload["complete"] is False
    assert payload["interruption"]["kind"] == "worker_crash"


def test_xdist_omits_trials_abandoned_after_worker_pool_shutdown(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        test_eval="""
import os
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [
    kensa_case(id="case_a", input="a"),
    kensa_case(id="case_b", input="b"),
    kensa_case(id="case_c", input="c"),
    kensa_case(id="case_d", input="d"),
])
def test_agent(case):
    if case.id == "case_a" and os.environ.get("PYTEST_XDIST_WORKER"):
        os._exit(3)
    assert case.input == case.id[-1]
"""
    )

    result = _run_kensa_xdist(
        pytester,
        "-q",
        "-n",
        "2",
        "--max-worker-restart=0",
        "--kensa-write-artifacts",
    )

    assert result.ret == 1
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    trials = {trial["case_id"]: trial for trial in payload["trials"]}
    assert "case_a" not in trials
    assert set(trials) <= {"case_b", "case_c", "case_d"}
    assert all(trial["status"] == "pass" for trial in trials.values())
    assert payload["complete"] is False
    assert payload["interruption"]["kind"] == "worker_crash"


def test_xdist_omits_trials_abandoned_after_maxfail(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [
    kensa_case(id=f"case_{letter}", input=letter)
    for letter in "abcdefgh"
])
def test_agent(case):
    assert case.id != "case_a"
"""
    )

    result = _run_kensa_xdist(
        pytester,
        "-q",
        "-n",
        "2",
        "--maxfail=1",
        "--kensa-write-artifacts",
    )

    assert result.ret == 1
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    trials = {trial["case_id"]: trial for trial in payload["trials"]}
    assert set(trials) < {f"case_{letter}" for letter in "abcdefgh"}
    assert trials["case_a"]["error_kind"] == "assertion"
    assert payload["complete"] is False
    assert payload["interruption"]["kind"] == "pytest_stopped"


def test_xdist_omits_setup_skip_and_setup_crash(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import pytest


@pytest.fixture
def skip_during_setup(case):
    if case.id == "case_skip":
        pytest.skip("not supported")
"""
    )
    pytester.makepyfile(
        test_eval="""
import os
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [
    kensa_case(id="case_skip", input="skip"),
    kensa_case(id="case_crash", input="crash"),
])
def test_agent(case, skip_during_setup):
    if case.id == "case_crash" and os.environ.get("PYTEST_XDIST_WORKER"):
        os._exit(3)
"""
    )

    result = _run_kensa_xdist(
        pytester,
        "-q",
        "-n",
        "2",
        "--kensa-write-artifacts",
    )

    assert result.ret == 1
    result.assert_outcomes(skipped=1, failed=1)
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    assert payload["trials"] == []
    assert payload["complete"] is False
    assert payload["interruption"]["kind"] == "worker_crash"


def test_xdist_role_helpers_and_controller_ingestion(tmp_path: Path) -> None:
    control_path = tmp_path / "state" / "run.json"
    write_control(
        control_path,
        WatchdogControl(
            run_id="run",
            result_path=tmp_path / "results" / "run.json",
            artifact_dir=tmp_path,
            default_timeout_s=30,
            expected_workers=1,
        ),
    )
    controller_options = {
        "--kensa-control-path": str(control_path),
        "numprocesses": None,
        "tx": [],
        "px": [],
        "dist": "no",
    }
    controller_config = SimpleNamespace(getoption=lambda name: controller_options.get(name, False))
    state = pytest_plugin._state(cast(Any, controller_config))
    pytest_plugin.pytest_sessionstart(cast(Any, SimpleNamespace(config=controller_config)))
    node = SimpleNamespace(
        config=controller_config,
        gateway=SimpleNamespace(spec=SimpleNamespace(popen=True)),
        workerinput={"workerid": "gw0"},
    )
    pytest_plugin.pytest_configure_node(cast(Any, node))
    worker_control = Path(node.workerinput["_kensa_control_path"])
    assert worker_control.is_file()
    assert set(node.workerinput) == {"workerid", "_kensa_control_path"}

    worker_config = SimpleNamespace(
        workerinput=node.workerinput,
        getoption=lambda name: False,
    )
    pytest_plugin.pytest_sessionstart(cast(Any, SimpleNamespace(config=worker_config)))
    items = [
        SimpleNamespace(
            nodeid=f"tests/evals/test_agent.py::test_agent[trial{trial_index}-case_a]",
            config=worker_config,
            callspec=SimpleNamespace(
                params={
                    pytest_plugin.PRIVATE_TRIAL: KensaTrial(trial_index, 2),
                    "case": kensa_case(id="case_a", input="hello"),
                }
            ),
            get_closest_marker=lambda name: (
                SimpleNamespace(args=[], kwargs={}) if name == "kensa" else None
            ),
        )
        for trial_index in (1, 2)
    ]
    runtime = pytest_plugin._runtime_for_item(cast(Any, items[0]))
    assert runtime is not None
    metadata = runtime.metadata(status="pass", duration_ms=1)
    report = SimpleNamespace(
        node=SimpleNamespace(config=controller_config),
        _kensa_trial_metadata=metadata.to_dict(),
        when="call",
    )

    pytest_plugin.pytest_runtest_logreport(cast(Any, report))
    pytest_plugin.pytest_runtest_logreport(cast(Any, SimpleNamespace()))

    assert state.trials == [metadata]
    crash_report = SimpleNamespace(
        node=SimpleNamespace(config=controller_config),
        nodeid=metadata.nodeid,
        when="???",
        longrepr="worker crashed",
    )
    pytest_plugin.pytest_runtest_logreport(cast(Any, crash_report))
    assert state.trials == [metadata]
    assert state.complete is False
    assert state.interruption == {
        "kind": "worker_crash",
        "message": "worker crashed",
        "nodeid": metadata.nodeid,
    }

    unknown_crash_report = SimpleNamespace(
        node=SimpleNamespace(config=controller_config),
        nodeid="unknown",
        when="???",
        longrepr="unknown worker crash",
    )
    pytest_plugin.pytest_runtest_logreport(cast(Any, unknown_crash_report))
    assert state.trials == [metadata]
    assert pytest_plugin._is_xdist_worker(cast(Any, controller_config)) is False

    assert pytest_plugin._is_xdist_worker(cast(Any, worker_config)) is True
    pytest_plugin._record_trial(cast(Any, worker_config), metadata)
    assert pytest_plugin._state(cast(Any, worker_config)).trials == [metadata]
    assert read_control(worker_control).trial_snapshot == metadata
    assert read_control(worker_control).active_trial is None
    pytest_plugin.pytest_sessionfinish(cast(Any, SimpleNamespace(config=worker_config)), 0)
    pytest_plugin.pytest_terminal_summary(
        cast(Any, SimpleNamespace()),
        0,
        cast(Any, worker_config),
    )
    pytest_plugin.pytest_testnodedown(cast(Any, node), None)
    assert not worker_control.exists()


def test_remote_xdist_nodes_are_untouched(tmp_path: Path) -> None:
    control_path = tmp_path / "state" / "run.json"
    write_control(
        control_path,
        WatchdogControl(
            run_id="run",
            result_path=tmp_path / "results" / "run.json",
            artifact_dir=tmp_path,
            default_timeout_s=30,
        ),
    )
    controller_config = SimpleNamespace(
        getoption=lambda name: str(control_path) if name == "--kensa-control-path" else None
    )
    node = SimpleNamespace(
        config=controller_config,
        gateway=SimpleNamespace(spec=SimpleNamespace(popen=False)),
        workerinput={"workerid": "gw0"},
    )

    pytest_plugin.pytest_configure_node(cast(Any, node))
    assert node.workerinput == {"workerid": "gw0"}


def test_controller_normalizes_worker_trial_payload() -> None:
    config = SimpleNamespace(getoption=lambda name: None)
    payload = {
        "nodeid": "tests/evals/test_agent.py::test_agent[trial1-case_a]",
        "group_id": "tests/evals/test_agent.py::test_agent[case_a]",
        "case_id": "case_a",
        "trial_index": 1,
        "configured_trials": 1,
        "status": "pass",
        "case": [],
        "trace": [],
        "judges": {},
        "active_operation": [],
    }
    report = SimpleNamespace(
        node=SimpleNamespace(config=config),
        _kensa_trial_metadata=payload,
        when="call",
    )

    pytest_plugin.pytest_runtest_logreport(cast(Any, report))

    trial = pytest_plugin._state(cast(Any, config)).trials[0]
    assert trial.case == {}
    assert trial.trace == {}
    assert trial.judges == []
    assert trial.active_operation is None


def test_control_file_invalid_run_id_names_source(tmp_path: Path) -> None:
    control_path = tmp_path / "state" / "run.json"
    write_control(
        control_path,
        WatchdogControl(
            run_id="../invalid",
            result_path=tmp_path / "results" / "run.json",
            artifact_dir=tmp_path,
            default_timeout_s=30,
        ),
    )
    config = SimpleNamespace(
        getoption=lambda name: str(control_path) if name == "--kensa-control-path" else None
    )

    with pytest.raises(pytest.UsageError, match="Kensa control file contains"):
        pytest_plugin.KensaSessionState(cast(Any, config))


def test_worker_configuration_validation() -> None:
    def config(
        numprocesses: int | None,
        tx: list[str],
        px: list[str] | None = None,
    ) -> Any:
        options = {"numprocesses": numprocesses, "tx": tx, "px": px or [], "dist": "load"}
        return SimpleNamespace(getoption=options.__getitem__)

    pytest_plugin._validate_worker_configuration(config(None, []), 1)
    pytest_plugin._validate_worker_configuration(config(4, ["popen"] * 4), 4)

    with pytest.raises(pytest.UsageError, match="expected 1 local pytest worker"):
        pytest_plugin._validate_worker_configuration(config(2, ["popen"] * 2), 1)
    with pytest.raises(pytest.UsageError, match="expected 4 local pytest workers"):
        pytest_plugin._validate_worker_configuration(config(4, ["popen"] * 2), 4)
    with pytest.raises(pytest.UsageError, match="local pytest workers only"):
        pytest_plugin._validate_worker_configuration(config(None, ["ssh=example.invalid"]), 1)
    with pytest.raises(pytest.UsageError, match="local pytest workers only"):
        pytest_plugin._validate_worker_configuration(
            config(None, [], ["socket=example.invalid:8888"]),
            1,
        )
    each_options = {
        "numprocesses": 2,
        "tx": ["popen", "popen"],
        "px": [],
        "dist": "each",
    }
    each_config = SimpleNamespace(getoption=each_options.__getitem__)
    with pytest.raises(pytest.UsageError, match="--dist=each is incompatible"):
        pytest_plugin._validate_worker_configuration(cast(Any, each_config), 2)
