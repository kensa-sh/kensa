from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from kensa.case import KensaCase, KensaMessage
from kensa.conversation import ConversationResponse
from kensa.target import (
    AgentRunEvidence,
    EffectPolicy,
    EvidenceCompleteness,
    ExecutionAttestation,
)
from kensa.target_command import (
    TARGET_PROTOCOL_VERSION,
    TargetTurnResult,
    serve_target,
    target_protocol_schema,
)

_ROOT = Path(__file__).parents[1]
_SCHEMA_PATH = _ROOT / "docs" / "protocol" / "target-command-v1.schema.json"
_REQUEST_TRANSCRIPT_PATH = _ROOT / "docs" / "protocol" / "target-command-v1.requests.jsonl"
_RESPONSE_TRANSCRIPT_PATH = _ROOT / "docs" / "protocol" / "target-command-v1.responses.jsonl"


def _request(**payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"))


def _run_host(requests: list[str], open_session: Any) -> tuple[int, list[dict[str, Any]], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = serve_target(
        open_session,
        stdin=io.StringIO("\n".join(requests) + "\n"),
        stdout=stdout,
        stderr=stderr,
    )
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    return exit_code, responses, stderr.getvalue()


def test_target_command_serves_one_case_aware_session_through_shutdown() -> None:
    calls: list[tuple[str, Any]] = []

    class Agent:
        def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            calls.append(("respond", messages))
            return ConversationResponse(content=f"turn-{len(messages)}", output={"turns": 1})

        def close(self) -> None:
            calls.append(("close", None))

    def open_session(case: KensaCase) -> Agent:
        calls.append(("open", dict(case.row)))
        return Agent()

    requests = [
        _request(
            type="handshake",
            request_id="request-1",
            version=TARGET_PROTOCOL_VERSION,
        ),
        _request(
            type="open_session",
            request_id="request-2",
            session_id="session-1",
            case={"id": "refund", "row": {"input": "help"}},
        ),
        _request(
            type="turn",
            request_id="request-3",
            session_id="session-1",
            messages=[{"role": "user", "content": "hello"}],
        ),
        _request(
            type="turn",
            request_id="request-4",
            session_id="session-1",
            messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "turn-1"},
                {"role": "user", "content": "again"},
            ],
        ),
        _request(
            type="close_session",
            request_id="request-5",
            session_id="session-1",
        ),
        _request(type="shutdown", request_id="request-6"),
    ]
    stdin = io.StringIO("\n".join(requests) + "\n")
    stdout = io.StringIO()
    stderr = io.StringIO()

    assert serve_target(open_session, stdin=stdin, stdout=stdout, stderr=stderr) == 0

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [response["type"] for response in responses] == [
        "handshake",
        "session_opened",
        "turn",
        "turn",
        "session_closed",
        "shutdown",
    ]
    assert responses[2]["response"] == {
        "content": "turn-1",
        "output": {"turns": 1},
        "termination_reason": None,
    }
    assert responses[3]["response"]["content"] == "turn-3"
    assert calls == [
        ("open", {"input": "help"}),
        ("respond", ({"role": "user", "content": "hello"},)),
        (
            "respond",
            (
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "turn-1"},
                {"role": "user", "content": "again"},
            ),
        ),
        ("close", None),
    ]
    assert stderr.getvalue() == ""


def test_async_target_factory_responder_and_cleanup_match_sync_wire_behavior() -> None:
    calls: list[str] = []
    loops: list[asyncio.AbstractEventLoop] = []

    class Agent:
        def __init__(self) -> None:
            self._loop = asyncio.get_running_loop()
            self._release = asyncio.Event()
            self._task = asyncio.create_task(self._release.wait())

        async def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            loop = asyncio.get_running_loop()
            loops.append(loop)
            assert loop is self._loop
            assert self._task.get_loop() is loop
            calls.append(f"respond:{len(messages)}")
            return ConversationResponse(content="async reply")

        async def close(self) -> None:
            loop = asyncio.get_running_loop()
            loops.append(loop)
            assert loop is self._loop
            self._release.set()
            await self._task
            calls.append("close")

    async def open_session(case: KensaCase) -> Agent:
        loops.append(asyncio.get_running_loop())
        calls.append(f"open:{case.id}")
        return Agent()

    exit_code, responses, stderr = _run_host(
        [
            _request(
                type="handshake",
                request_id="request-1",
                version=TARGET_PROTOCOL_VERSION,
            ),
            _request(
                type="open_session",
                request_id="request-2",
                session_id="session-1",
                case={"id": "async-case", "row": {}},
            ),
            _request(
                type="turn",
                request_id="request-3",
                session_id="session-1",
                messages=[],
            ),
            _request(
                type="turn",
                request_id="request-4",
                session_id="session-1",
                messages=[
                    {"role": "assistant", "content": "async reply"},
                    {"role": "user", "content": "again"},
                ],
            ),
            _request(
                type="close_session",
                request_id="request-5",
                session_id="session-1",
            ),
            _request(type="shutdown", request_id="request-6"),
        ],
        open_session,
    )

    assert exit_code == 0
    assert [response["type"] for response in responses] == [
        "handshake",
        "session_opened",
        "turn",
        "turn",
        "session_closed",
        "shutdown",
    ]
    assert responses[2]["response"] == {
        "content": "async reply",
        "output": None,
        "termination_reason": None,
    }
    assert calls == ["open:async-case", "respond:0", "respond:2", "close"]
    assert len({id(loop) for loop in loops}) == 1
    assert loops[0].is_closed()
    assert stderr == ""


def test_invalid_requests_fail_closed_without_additional_target_calls() -> None:
    calls: list[str] = []

    class Agent:
        def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            calls.append("respond")
            return ConversationResponse(content="unused")

        def close(self) -> None:
            calls.append("close")

    def open_session(case: KensaCase) -> Agent:
        calls.append(f"open:{case.id}")
        return Agent()

    exit_code, responses, stderr = _run_host(
        [
            "{",
            _request(
                type="handshake",
                request_id="unknown-field",
                version=TARGET_PROTOCOL_VERSION,
                extra=True,
            ),
            _request(
                type="open_session",
                request_id="before-handshake",
                session_id="session-1",
                case={"id": "case", "row": {}},
            ),
            _request(
                type="handshake",
                request_id="unsupported",
                version="kensa.target.v2",
            ),
            _request(
                type="handshake",
                request_id="handshake",
                version=TARGET_PROTOCOL_VERSION,
            ),
            _request(
                type="turn",
                request_id="before-open",
                session_id="session-1",
                messages=[],
            ),
            _request(
                type="open_session",
                request_id="open",
                session_id="session-1",
                case={"id": "case", "row": {}},
            ),
            _request(
                type="open_session",
                request_id="duplicate-open",
                session_id="session-2",
                case={"id": "second", "row": {}},
            ),
            _request(
                type="turn",
                request_id="wrong-session",
                session_id="session-2",
                messages=[],
            ),
            _request(
                type="turn",
                request_id="invalid-message",
                session_id="session-1",
                messages=[{"role": "user", "content": "hello", "extra": True}],
            ),
            _request(
                type="close_session",
                request_id="close",
                session_id="session-1",
            ),
            _request(type="shutdown", request_id="shutdown"),
        ],
        open_session,
    )

    assert exit_code == 0
    assert [response.get("code", response["type"]) for response in responses] == [
        "invalid_json",
        "invalid_request",
        "invalid_state",
        "unsupported_version",
        "handshake",
        "invalid_state",
        "session_opened",
        "invalid_state",
        "session_mismatch",
        "invalid_request",
        "session_closed",
        "shutdown",
    ]
    assert calls == ["open:case", "close"]
    assert stderr == ""


def test_protocol_rejects_remaining_invalid_states_and_message_shapes() -> None:
    calls: list[str] = []

    class Agent:
        def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            calls.append("respond")
            return ConversationResponse(content="tool history accepted")

        def close(self) -> None:
            calls.append("close")

    def open_session(case: KensaCase) -> Agent:
        calls.append(f"open:{case.id}")
        return Agent()

    exit_code, responses, stderr = _run_host(
        [
            "[]",
            '{"type":"handshake","request_id":"nan","version":NaN}',
            _request(type="handshake", request_id=" ", version=TARGET_PROTOCOL_VERSION),
            _request(
                type="turn",
                request_id="turn-before-handshake",
                session_id="session",
                messages=[],
            ),
            _request(type="shutdown", request_id="shutdown-before-handshake"),
            _request(
                type="handshake",
                request_id="handshake",
                version=TARGET_PROTOCOL_VERSION,
            ),
            _request(type="shutdown", request_id="handshake"),
            _request(
                type="handshake",
                request_id="duplicate-handshake",
                version=TARGET_PROTOCOL_VERSION,
            ),
            _request(
                type="close_session",
                request_id="close-before-open",
                session_id="session",
            ),
            _request(
                type="open_session",
                request_id="invalid-case",
                session_id="session",
                case={
                    "id": "invalid-case",
                    "row": {
                        "messages": [
                            {"role": "tool", "tool_call_id": "missing", "content": "result"}
                        ]
                    },
                },
            ),
            _request(
                type="open_session",
                request_id="open",
                session_id="session",
                case={"id": "case", "row": {}},
            ),
            _request(type="shutdown", request_id="shutdown-active"),
            _request(
                type="turn",
                request_id="orphan-tool-result",
                session_id="session",
                messages=[{"role": "tool", "tool_call_id": "missing", "content": "result"}],
            ),
            _request(
                type="turn",
                request_id="empty-assistant",
                session_id="session",
                messages=[{"role": "assistant"}],
            ),
            _request(
                type="turn",
                request_id="malformed-tool-arguments",
                session_id="session",
                messages=[
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{"},
                            }
                        ],
                    }
                ],
            ),
            _request(
                type="turn",
                request_id="non-object-tool-arguments",
                session_id="session",
                messages=[
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "[]"},
                            }
                        ],
                    }
                ],
            ),
            _request(
                type="turn",
                request_id="valid-tool-history",
                session_id="session",
                messages=[
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call-1", "content": "result"},
                ],
            ),
            _request(
                type="close_session",
                request_id="close",
                session_id="session",
            ),
            _request(
                type="open_session",
                request_id="open-after-close",
                session_id="other-session",
                case={"id": "other-case", "row": {}},
            ),
            _request(
                type="close_session",
                request_id="duplicate-close",
                session_id="session",
            ),
            _request(type="shutdown", request_id="shutdown"),
        ],
        open_session,
    )

    assert exit_code == 0
    assert [response.get("code", response["type"]) for response in responses] == [
        "invalid_request",
        "invalid_json",
        "invalid_request",
        "invalid_state",
        "invalid_state",
        "handshake",
        "duplicate_request_id",
        "invalid_state",
        "invalid_state",
        "invalid_request",
        "session_opened",
        "invalid_state",
        "invalid_messages",
        "invalid_request",
        "invalid_request",
        "invalid_request",
        "turn",
        "session_closed",
        "invalid_state",
        "invalid_state",
        "shutdown",
    ]
    assert calls == ["open:case", "respond", "close"]
    assert stderr == ""


def test_turn_requires_complete_accepted_message_history() -> None:
    calls: list[tuple[KensaMessage, ...]] = []

    class Agent:
        def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            calls.append(messages)
            return ConversationResponse(content=f"reply-{len(calls)}")

    exit_code, responses, _ = _run_host(
        [
            _request(
                type="handshake",
                request_id="handshake",
                version=TARGET_PROTOCOL_VERSION,
            ),
            _request(
                type="open_session",
                request_id="open",
                session_id="session",
                case={
                    "id": "history",
                    "row": {"messages": [{"role": "user", "content": "first"}]},
                },
            ),
            _request(
                type="turn",
                request_id="first-turn",
                session_id="session",
                messages=[{"role": "user", "content": "first"}],
            ),
            _request(
                type="turn",
                request_id="missing-history",
                session_id="session",
                messages=[{"role": "user", "content": "second"}],
            ),
            _request(
                type="turn",
                request_id="complete-history",
                session_id="session",
                messages=[
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "reply-1"},
                    {"role": "user", "content": "second"},
                ],
            ),
            _request(
                type="close_session",
                request_id="close",
                session_id="session",
            ),
            _request(type="shutdown", request_id="shutdown"),
        ],
        lambda case: Agent(),
    )

    assert exit_code == 0
    assert responses[3]["code"] == "history_mismatch"
    assert len(calls) == 2
    assert len(calls[1]) == 3


def test_turn_serializes_valid_external_run_evidence() -> None:
    evidence = AgentRunEvidence(
        run_id="external-run",
        attestation=ExecutionAttestation(
            revision="abc123",
            environment="test",
            effects=EffectPolicy.SANDBOXED,
        ),
        trajectory_completeness=EvidenceCompleteness.COMPLETE,
        state_completeness=EvidenceCompleteness.COMPLETE,
    )

    class Agent:
        def respond(self, messages: tuple[KensaMessage, ...]) -> TargetTurnResult:
            return TargetTurnResult(
                response=ConversationResponse(content="evidenced"),
                evidence=evidence,
            )

    exit_code, responses, stderr = _run_host(
        [
            _request(
                type="handshake",
                request_id="handshake",
                version=TARGET_PROTOCOL_VERSION,
            ),
            _request(
                type="open_session",
                request_id="open",
                session_id="session",
                case={"id": "evidence", "row": {}},
            ),
            _request(
                type="turn",
                request_id="turn",
                session_id="session",
                messages=[],
            ),
            _request(
                type="close_session",
                request_id="close",
                session_id="session",
            ),
            _request(type="shutdown", request_id="shutdown"),
        ],
        lambda case: Agent(),
    )

    assert exit_code == 0
    assert responses[2]["evidence"] == evidence.model_dump(mode="json")
    assert stderr == ""


def test_invalid_response_fails_the_session_without_retrying_target_code() -> None:
    calls = 0

    class Agent:
        def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            nonlocal calls
            calls += 1
            return ConversationResponse(output=object())

    exit_code, responses, stderr = _run_host(
        [
            _request(
                type="handshake",
                request_id="handshake",
                version=TARGET_PROTOCOL_VERSION,
            ),
            _request(
                type="open_session",
                request_id="open",
                session_id="session",
                case={"id": "invalid-response", "row": {}},
            ),
            _request(
                type="turn",
                request_id="turn",
                session_id="session",
                messages=[],
            ),
            _request(
                type="turn",
                request_id="no-retry",
                session_id="session",
                messages=[],
            ),
            _request(
                type="close_session",
                request_id="close",
                session_id="session",
            ),
            _request(type="shutdown", request_id="shutdown"),
        ],
        lambda case: Agent(),
    )

    assert exit_code == 0
    assert responses[2]["code"] == "invalid_response"
    assert responses[3]["code"] == "invalid_state"
    assert calls == 1
    assert "target turn failed" in stderr


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        ("not a response", "invalid_response"),
        (ValueError("target value error"), "target_turn_failed"),
    ],
)
def test_turn_distinguishes_target_exceptions_from_response_contracts(
    response: object,
    error_code: str,
) -> None:
    class Agent:
        def respond(self, messages: tuple[KensaMessage, ...]) -> Any:
            if isinstance(response, Exception):
                raise response
            return response

    exit_code, responses, stderr = _run_host(
        [
            _request(
                type="handshake",
                request_id="handshake",
                version=TARGET_PROTOCOL_VERSION,
            ),
            _request(
                type="open_session",
                request_id="open",
                session_id="session",
                case={"id": "response-contract", "row": {}},
            ),
            _request(
                type="turn",
                request_id="turn",
                session_id="session",
                messages=[],
            ),
            _request(
                type="close_session",
                request_id="close",
                session_id="session",
            ),
            _request(type="shutdown", request_id="shutdown"),
        ],
        lambda case: Agent(),
    )

    assert exit_code == 0
    assert responses[2]["code"] == error_code
    assert "target turn failed" in stderr


def test_open_rejects_a_session_without_a_responder() -> None:
    exit_code, responses, stderr = _run_host(
        [
            _request(
                type="handshake",
                request_id="handshake",
                version=TARGET_PROTOCOL_VERSION,
            ),
            _request(
                type="open_session",
                request_id="open",
                session_id="session",
                case={"id": "invalid-session", "row": {}},
            ),
            _request(type="shutdown", request_id="shutdown"),
        ],
        lambda case: object(),
    )

    assert exit_code == 0
    assert responses[1]["code"] == "target_open_failed"
    assert "must provide respond" in stderr


def test_close_reports_non_callable_and_raising_cleanup() -> None:
    class NonCallableClose:
        close = "not callable"

        def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            return ConversationResponse(content="ok")

    class RaisingClose:
        def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            return ConversationResponse(content="ok")

        def close(self) -> None:
            raise RuntimeError("cleanup exploded")

    requests = [
        _request(type="handshake", request_id="handshake", version=TARGET_PROTOCOL_VERSION),
        _request(
            type="open_session",
            request_id="open",
            session_id="session",
            case={"id": "cleanup", "row": {}},
        ),
        _request(
            type="close_session",
            request_id="close",
            session_id="session",
        ),
        _request(type="shutdown", request_id="shutdown"),
    ]

    for session in (NonCallableClose(), RaisingClose()):
        exit_code, responses, stderr = _run_host(
            requests,
            lambda case, current=session: current,
        )
        assert exit_code == 0
        assert responses[2]["code"] == "target_close_failed"
        assert "target session close failed" in stderr


@pytest.mark.parametrize(
    ("phase", "error_code"),
    [
        ("open", "target_open_failed"),
        ("turn", "target_turn_failed"),
        ("close", "target_close_failed"),
    ],
)
def test_target_exceptions_use_protocol_stdout_and_diagnostic_stderr(
    phase: str,
    error_code: str,
) -> None:
    script = textwrap.dedent(
        """
        import sys

        from kensa.conversation import ConversationResponse
        from kensa.target_command import serve_target


        phase = sys.argv[1]


        class Agent:
            def respond(self, messages):
                print("repository turn diagnostic", file=sys.stderr)
                if phase == "turn":
                    raise RuntimeError("turn exploded")
                return ConversationResponse(content="ok")

            def close(self):
                print("repository close diagnostic", file=sys.stderr)
                if phase == "close":
                    raise RuntimeError("close exploded")


        def open_session(case):
            print("repository open diagnostic", file=sys.stderr)
            if phase == "open":
                raise RuntimeError("open exploded")
            return Agent()


        raise SystemExit(serve_target(open_session))
        """
    )
    requests = [
        _request(type="handshake", request_id="handshake", version=TARGET_PROTOCOL_VERSION),
        _request(
            type="open_session",
            request_id="open",
            session_id="session",
            case={"id": "exception", "row": {}},
        ),
    ]
    if phase != "open":
        requests.append(
            _request(
                type="turn",
                request_id="turn",
                session_id="session",
                messages=[],
            )
        )
        requests.append(
            _request(
                type="close_session",
                request_id="close",
                session_id="session",
            )
        )
    requests.append(_request(type="shutdown", request_id="shutdown"))

    completed = subprocess.run(
        [sys.executable, "-c", script, phase],
        input="\n".join(requests) + "\n",
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert error_code in [response.get("code") for response in responses]
    assert all(
        response["type"]
        in {"handshake", "session_opened", "turn", "session_closed", "error", "shutdown"}
        for response in responses
    )
    assert "repository" not in completed.stdout
    assert "repository" in completed.stderr
    assert "failed" in completed.stderr


def test_eof_closes_an_active_session_and_returns_failure() -> None:
    closed = 0

    class Agent:
        def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            return ConversationResponse(content="unused")

        def close(self) -> None:
            nonlocal closed
            closed += 1

    exit_code, responses, stderr = _run_host(
        [
            _request(
                type="handshake",
                request_id="handshake",
                version=TARGET_PROTOCOL_VERSION,
            ),
            _request(
                type="open_session",
                request_id="open",
                session_id="session",
                case={"id": "eof", "row": {}},
            ),
        ],
        lambda case: Agent(),
    )

    assert exit_code == 1
    assert [response["type"] for response in responses] == ["handshake", "session_opened"]
    assert closed == 1
    assert "EOF before shutdown" in stderr


def test_protocol_schema_and_transcripts_are_canonical_and_language_neutral() -> None:
    schema = json.loads(_SCHEMA_PATH.read_text())

    assert schema == target_protocol_schema()
    Draft202012Validator.check_schema(schema)
    request_validator = Draft202012Validator(schema["$defs"]["request"])
    response_validator = Draft202012Validator(schema["$defs"]["response"])
    for line in _REQUEST_TRANSCRIPT_PATH.read_text().splitlines():
        request_validator.validate(json.loads(line))
    for line in _RESPONSE_TRANSCRIPT_PATH.read_text().splitlines():
        response_validator.validate(json.loads(line))


def test_target_host_adds_no_agent_framework_or_process_dependency() -> None:
    source = (_ROOT / "src" / "kensa" / "target_command.py").read_text()
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    dependencies = project["project"]["dependencies"]

    assert not any("jsonschema" in dependency for dependency in dependencies)
    assert "import pytest" not in source
    assert "import subprocess" not in source
    assert "import requests" not in source
    assert "import httpx" not in source
    assert "import openai" not in source
    assert "import anthropic" not in source
    assert "import langfuse" not in source
