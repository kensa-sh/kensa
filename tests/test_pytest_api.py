from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from kensa.case import KensaCaseError, KensaMessage, kensa_case


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
    result.stdout.fnmatch_lines(["*FAIL *test_agent*"])


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
    result.stdout.fnmatch_lines(["*FLAKY *test_agent*"])


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
    result.stdout.fnmatch_lines(["*PARTIAL *test_agent*"])


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


@pytest.fixture
def kensa_run():
    return lambda case: {"ok": True}


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
    assert case.run(kensa_run) == {"ok": True}
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


@pytest.fixture
def kensa_run():
    return lambda case: {"input": case.input}
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run):
    output = case.run(kensa_run)
    assert output["input"] == "hello"
"""
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(passed=1)
    artifacts = list((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text())
    assert payload["trials"][0]["output"] == {"input": "hello"}
    assert "type" not in payload["trials"][0]
    assert "type" not in payload["aggregates"][0]
    trace_artifact = next(
        (Path(str(pytester.path)) / ".kensa" / "traces" / "runs").glob("*/trials.jsonl")
    )
    trace_row = json.loads(trace_artifact.read_text().splitlines()[0])
    assert "type" not in trace_row


def test_failed_assertions_still_write_trial_metadata(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import pytest


@pytest.fixture
def kensa_run():
    return lambda case: {"input": case.input}
"""
    )
    pytester.makepyfile(
        test_eval="""
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="case_a", input="hello")])
def test_agent(case, kensa_run):
    assert case.run(kensa_run) == {"input": "hello"}
    assert False, "regression still present"
"""
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(failed=1)
    artifact = next((Path(str(pytester.path)) / ".kensa" / "results").glob("*.json"))
    payload = json.loads(artifact.read_text())
    assert payload["trials"][0]["status"] == "fail"
    assert payload["trials"][0]["output"] == {"input": "hello"}
    assert payload["trials"][0]["error_kind"] == "assertion"


def test_case_run_rejects_second_call(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import pytest


@pytest.fixture
def kensa_run():
    return lambda case: "ok"
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


@pytest.fixture
def kensa_run():
    async def _run(case):
        return {"value": case.input}
    return _run
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
    assert output == {"value": "hello"}
"""
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=1)
