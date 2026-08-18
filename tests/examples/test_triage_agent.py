from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from examples.triage_agent.agent import TooManyToolRoundsError, TriageAgent


class FakeFunction:
    def __init__(self, name: str, arguments: dict) -> None:
        self.name = name
        self.arguments = json.dumps(arguments)


class FakeToolCall:
    def __init__(self, id_: str, name: str, arguments: dict) -> None:
        self.id = id_
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, content: str | None = None, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self) -> dict:
        return {
            "role": "assistant",
            "content": self.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in self.tool_calls
            ]
            or None,
        }


class FakeChoice:
    def __init__(self, message: FakeMessage, finish_reason: str) -> None:
        self.message = message
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, message: FakeMessage, finish_reason: str) -> None:
        self.choices = [FakeChoice(message, finish_reason)]


def _agent() -> TriageAgent:
    return TriageAgent(api_key="test-key", model="gpt-5-4-mini", provider="openai")


def test_run_returns_reply_with_no_tool_call() -> None:
    response = FakeResponse(FakeMessage(content="All services are healthy."), "stop")

    with patch("examples.triage_agent.agent.completion", return_value=response) as mock_completion:
        result = _agent().run([{"role": "user", "content": "Any incidents?"}])

    assert result == "All services are healthy."
    mock_completion.assert_called_once()


def test_run_executes_tool_round_trip_and_returns_final_reply() -> None:
    tool_call = FakeToolCall("call_1", "check_service_status", {"service": "checkout-service"})
    first = FakeResponse(FakeMessage(tool_calls=[tool_call]), "tool_calls")
    second = FakeResponse(
        FakeMessage(content="checkout-service is degraded; paging on-call."), "stop"
    )

    with patch(
        "examples.triage_agent.agent.completion", side_effect=[first, second]
    ) as mock_completion:
        result = _agent().run([{"role": "user", "content": "checkout-service is throwing errors"}])

    assert result == "checkout-service is degraded; paging on-call."
    assert mock_completion.call_count == 2

    second_call_messages = mock_completion.call_args_list[1].kwargs["messages"]
    tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert json.loads(tool_messages[0]["content"]) == {
        "status": "degraded",
        "region": "us-east-1",
    }


def test_run_stops_after_tool_round_cap() -> None:
    tool_call = FakeToolCall("call_x", "check_service_status", {"service": "checkout-service"})
    looping_response = FakeResponse(FakeMessage(tool_calls=[tool_call]), "tool_calls")

    with (
        patch("examples.triage_agent.agent.completion", return_value=looping_response),
        pytest.raises(TooManyToolRoundsError),
    ):
        _agent().run([{"role": "user", "content": "checkout-service is throwing errors"}])
