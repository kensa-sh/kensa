"""On-call incident triage agent.

Eval-bootstrap target agent for this repo: a small, real conversational agent that
Kensa's own eval harness can wire into, since Kensa's repository otherwise has no
production agent to test. Not a product feature.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from any_llm import completion

MAX_TOOL_ROUNDS = 4

_SERVICE_STATUS = {
    "checkout-service": {"status": "degraded", "region": "us-east-1"},
    "payments-service": {"status": "healthy", "region": "us-east-1"},
    "auth-service": {"status": "healthy", "region": "us-east-1"},
}

_ERROR_RATES = {
    "checkout-service": {"5m": 0.42, "15m": 0.31, "60m": 0.08},
    "payments-service": {"5m": 0.01, "15m": 0.01, "60m": 0.01},
    "auth-service": {"5m": 0.00, "15m": 0.00, "60m": 0.00},
}


class TooManyToolRoundsError(RuntimeError):
    """Raised when the model keeps requesting tools past the round cap."""


def check_service_status(service: str) -> dict:
    """Look up the current health status and region for a service.

    Args:
        service: The service name, e.g. "checkout-service".

    Returns:
        A dict with "status" ("healthy" or "degraded") and "region".
    """
    return _SERVICE_STATUS.get(service, {"status": "unknown", "region": "unknown"})


def query_error_rate(service: str, window_minutes: int) -> dict:
    """Look up the recent error rate for a service.

    Args:
        service: The service name, e.g. "checkout-service".
        window_minutes: The lookback window in minutes (5, 15, or 60).

    Returns:
        A dict with "error_rate" as a fraction between 0 and 1.
    """
    rates = _ERROR_RATES.get(service, {})
    key = f"{window_minutes}m"
    return {"error_rate": rates.get(key, 0.0)}


def page_oncall(service: str, severity: str) -> dict:
    """Page the on-call engineer for a service.

    This is a fake acknowledgement for evaluation purposes; it never sends a real
    page.

    Args:
        service: The service name, e.g. "checkout-service".
        severity: One of "low", "medium", "high", "critical".

    Returns:
        A dict with "paged" (bool) and "ack_id" (str).
    """
    return {"paged": True, "ack_id": f"ack-{service}-{severity}"}


_TOOLS = [check_service_status, query_error_rate, page_oncall]
_TOOLS_BY_NAME = {tool.__name__: tool for tool in _TOOLS}

_SYSTEM_PROMPT = (
    "You are an on-call incident triage assistant. Given an alert, use the "
    "available tools to check service status and error rates before deciding "
    "whether to page on-call. Only page for a confirmed degraded or unhealthy "
    "service with an elevated error rate. Explain your reasoning briefly in your "
    "final reply."
)


@dataclass
class TriageAgent:
    """Case-aware on-call triage agent backed by a real model call."""

    api_key: str
    model: str
    provider: str

    def run(self, messages: list[dict]) -> str:
        """Start one conversation and return the agent's final reply text."""

        conversation = [{"role": "system", "content": _SYSTEM_PROMPT}, *messages]

        for _ in range(MAX_TOOL_ROUNDS):
            response = completion(
                model=self.model,
                provider=self.provider,
                messages=conversation,
                tools=_TOOLS,
                api_key=self.api_key,
            )
            choice = response.choices[0]
            message = choice.message
            tool_calls = message.tool_calls or []

            if not tool_calls:
                return message.content or ""

            conversation.append(message.model_dump())
            for call in tool_calls:
                tool = _TOOLS_BY_NAME[call.function.name]
                arguments = json.loads(call.function.arguments)
                result = tool(**arguments)
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result),
                    }
                )

        raise TooManyToolRoundsError(
            f"Model requested tools for {MAX_TOOL_ROUNDS} rounds without finishing."
        )
