from __future__ import annotations

import io
import json
from typing import Any

import pytest

from kensa import target_command
from kensa.case import KensaCase, KensaMessage
from kensa.conversation import ConversationResponse
from kensa.target_command import TARGET_PROTOCOL_VERSION, serve_target


def _request(**payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"))


def _run_host(
    requests: list[str],
    open_session: Any,
) -> tuple[int, list[dict[str, Any]], str]:
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


def test_protocol_rejects_overflowing_numbers_without_invoking_target() -> None:
    calls: list[str] = []

    class Agent:
        def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            calls.append("respond")
            return ConversationResponse(content="unexpected")

        def close(self) -> None:
            calls.append("close")

    def open_session(case: KensaCase) -> Agent:
        calls.append(f"open:{case.id}")
        return Agent()

    exit_code, responses, stderr = _run_host(
        [
            _request(
                type="handshake",
                request_id="handshake",
                version=TARGET_PROTOCOL_VERSION,
            ),
            (
                '{"type":"open_session","request_id":"overflow-open",'
                '"session_id":"session","case":{"id":"case","row":{"value":1e999}}}'
            ),
            _request(
                type="open_session",
                request_id="open",
                session_id="session",
                case={"id": "case", "row": {}},
            ),
            _request(
                type="turn",
                request_id="overflow-arguments",
                session_id="session",
                messages=[
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": '{"value":1e999}',
                                },
                            }
                        ],
                    }
                ],
            ),
            _request(type="close_session", request_id="close", session_id="session"),
            _request(type="shutdown", request_id="shutdown"),
        ],
        open_session,
    )

    assert exit_code == 0
    assert [response.get("code", response["type"]) for response in responses] == [
        "handshake",
        "invalid_json",
        "session_opened",
        "invalid_request",
        "session_closed",
        "shutdown",
    ]
    assert responses[1]["request_id"] == "overflow-open"
    assert calls == ["open:case", "close"]
    assert stderr == ""


def test_protocol_parses_each_request_frame_once(monkeypatch: pytest.MonkeyPatch) -> None:
    loads = target_command.json.loads
    calls = 0

    def counted_loads(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return loads(*args, **kwargs)

    monkeypatch.setattr(target_command.json, "loads", counted_loads)

    request = target_command._parse_request(
        _request(
            type="handshake",
            request_id="handshake",
            version=TARGET_PROTOCOL_VERSION,
        )
    )

    assert request.request_id == "handshake"
    assert calls == 1


def test_protocol_bounds_request_id_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Agent:
        def respond(self, messages: tuple[KensaMessage, ...]) -> ConversationResponse:
            calls.append("respond")
            return ConversationResponse(content="unexpected")

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(target_command, "_MAX_REQUESTS_PER_PROCESS", 2)
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
                case={"id": "case", "row": {}},
            ),
            _request(
                type="turn",
                request_id="over-limit",
                session_id="session",
                messages=[],
            ),
        ],
        lambda case: calls.append(f"open:{case.id}") or Agent(),
    )

    assert exit_code == 1
    assert [response.get("code", response["type"]) for response in responses] == [
        "handshake",
        "session_opened",
        "request_limit_exceeded",
    ]
    assert responses[-1]["fatal"] is True
    assert calls == ["open:case", "close"]
    assert "EOF before shutdown" in stderr
