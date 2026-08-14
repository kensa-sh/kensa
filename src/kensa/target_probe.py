"""Behavioral readiness verification for configured command targets."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from kensa.case import KensaCase, kensa_case
from kensa.conversation import ConversationResponse
from kensa.errors import KensaCaseError, KensaEvalError
from kensa.models import KensaProjectConfig
from kensa.target import AgentRunEvidence
from kensa.target_client import TargetCommandSession

TargetProbeBoundary = Literal[
    "configuration",
    "startup",
    "handshake",
    "session_open",
    "turn",
    "response",
    "evidence",
    "effect_policy",
    "cleanup",
    "timeout",
]


@dataclass(frozen=True)
class TargetProbeFailure:
    boundary: TargetProbeBoundary
    message: str
    kind: str
    operation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "boundary": self.boundary,
            "message": self.message,
            "kind": self.kind,
            "operation": self.operation,
        }


@dataclass
class TargetProbeResult:
    requested: bool
    configured: bool
    attempted: bool
    ready: bool
    command: tuple[str, ...] | None
    case_path: Path | None
    allow_live_effects: bool
    case_id: str | None = None
    observed_lifecycle: list[str] = field(default_factory=list)
    response_non_empty: bool = False
    evidence: AgentRunEvidence | None = None
    failure: TargetProbeFailure | None = None
    cleanup_failure: TargetProbeFailure | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "configured": self.configured,
            "attempted": self.attempted,
            "ready": self.ready,
            "command": list(self.command) if self.command is not None else None,
            "case_path": str(self.case_path) if self.case_path is not None else None,
            "case_id": self.case_id,
            "allow_live_effects": self.allow_live_effects,
            "observed_lifecycle": list(self.observed_lifecycle),
            "response_non_empty": self.response_non_empty,
            "attestation": (
                self.evidence.attestation.model_dump(mode="json")
                if self.evidence is not None
                else None
            ),
            "evidence": (
                {
                    "run_id": self.evidence.run_id,
                    "trajectory_completeness": self.evidence.trajectory_completeness.value,
                    "state_completeness": self.evidence.state_completeness.value,
                    "incomplete_reason": self.evidence.incomplete_reason,
                }
                if self.evidence is not None
                else None
            ),
            "failure": self.failure.to_dict() if self.failure is not None else None,
            "cleanup_failure": (
                self.cleanup_failure.to_dict() if self.cleanup_failure is not None else None
            ),
        }


def verify_configured_target(
    config: KensaProjectConfig,
    *,
    case_path: Path | None,
    allow_live_effects: bool,
    cwd: Path,
    config_error: str | None = None,
) -> TargetProbeResult:
    requested = (
        config_error is not None or config.target_command is not None or case_path is not None
    )
    result = TargetProbeResult(
        requested=requested,
        configured=config.target_command is not None,
        attempted=False,
        ready=False,
        command=config.target_command,
        case_path=case_path,
        allow_live_effects=allow_live_effects,
    )
    if not requested:
        return result
    if config_error is not None:
        result.failure = TargetProbeFailure(
            boundary="configuration",
            message=config_error,
            kind="invalid_configuration",
        )
        return result
    if config.target_command is None:
        result.failure = TargetProbeFailure(
            boundary="configuration",
            message="no target_command is configured in [tool.kensa]",
            kind="missing_command",
        )
        return result
    if case_path is None:
        result.failure = TargetProbeFailure(
            boundary="configuration",
            message="a readiness case is required; pass --target-case PATH",
            kind="missing_case",
        )
        return result
    try:
        case = _readiness_case(case_path)
    except (OSError, ValueError, KensaCaseError) as exc:
        result.failure = TargetProbeFailure(
            boundary="configuration",
            message=f"readiness case is invalid: {exc}",
            kind="invalid_case",
        )
        return result

    result.case_id = case.id
    result.attempted = True
    session = TargetCommandSession(
        config.target_command,
        timeout_s=config.target_timeout_s,
        cwd=cwd,
    )
    turn_completed = False
    try:
        session.open(case)
        result.observed_lifecycle.extend(("startup", "handshake", "session_open"))
        messages = tuple(case.messages) if "messages" in case.row else ()
        response = session.respond(messages)
        turn_completed = True
        result.observed_lifecycle.extend(("turn", "response"))
        result.response_non_empty = _response_non_empty(response)
        if not result.response_non_empty:
            result.failure = TargetProbeFailure(
                boundary="response",
                message="target returned an empty response; provide content or output",
                kind="empty_response",
                operation="turn",
            )
        else:
            _validate_evidence(result, session.last_evidence, allow_live_effects)
    except KensaEvalError as exc:
        for stage in _lifecycle_before_failure(exc):
            if stage not in result.observed_lifecycle:
                result.observed_lifecycle.append(stage)
        result.failure = _eval_failure(exc)
    except RuntimeError as exc:
        result.observed_lifecycle.append("turn")
        result.failure = TargetProbeFailure(
            boundary="turn",
            message=str(exc) or "target turn failed",
            kind="target_turn_failed",
            operation="turn",
        )
    finally:
        try:
            session.close()
            if turn_completed:
                result.observed_lifecycle.append("cleanup")
        except KensaEvalError as exc:
            cleanup = TargetProbeFailure(
                boundary="cleanup",
                message=str(exc),
                kind=exc.failure.kind,
                operation=str(exc.failure.evidence.get("operation", "cleanup")),
            )
            if result.failure is None:
                result.failure = cleanup
            else:
                result.cleanup_failure = cleanup

    result.ready = result.failure is None and result.cleanup_failure is None
    return result


def _validate_evidence(
    result: TargetProbeResult,
    evidence: AgentRunEvidence | None,
    allow_live_effects: bool,
) -> None:
    if evidence is None:
        result.failure = TargetProbeFailure(
            boundary="evidence",
            message="target returned no execution evidence or attestation",
            kind="missing_evidence",
            operation="turn",
        )
        return
    result.evidence = evidence
    result.observed_lifecycle.append("evidence")
    if evidence.attestation.effects.value == "live" and not allow_live_effects:
        result.failure = TargetProbeFailure(
            boundary="effect_policy",
            message=(
                "target attested live effects; use a safe effect adapter or pass "
                "--allow-live-target-effects"
            ),
            kind="live_effects_not_allowed",
            operation="turn",
        )
        return
    result.observed_lifecycle.append("effect_policy")


def _response_non_empty(response: ConversationResponse) -> bool:
    return response.content is not None or response.output not in (None, "", [], {})


def _readiness_case(path: Path) -> KensaCase:
    raw = path.read_text()
    payload = json.loads(raw, parse_constant=_reject_json_constant)
    json.dumps(payload, allow_nan=False)
    if not isinstance(payload, dict):
        raise ValueError("file must contain a JSON object")
    values = dict(payload)
    case_id = values.pop("id", None)
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("id must contain non-whitespace text")
    return kensa_case(id=case_id, **values)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _lifecycle_before_failure(error: KensaEvalError) -> list[str]:
    operation = str(error.failure.evidence.get("operation", ""))
    if operation == "handshake":
        return ["startup"]
    if operation == "open_session":
        return ["startup", "handshake"]
    if operation == "turn":
        return ["startup", "handshake", "session_open", "turn"]
    return []


def _eval_failure(error: KensaEvalError) -> TargetProbeFailure:
    operation = str(error.failure.evidence.get("operation", ""))
    if error.failure.kind == "target_timeout":
        boundary: TargetProbeBoundary = "timeout"
    elif operation == "startup":
        boundary = "startup"
    elif operation == "handshake":
        boundary = "handshake"
    elif operation == "open_session":
        boundary = "session_open"
    elif operation == "turn" and error.failure.kind == "target_protocol":
        boundary = _validation_boundary(error)
    else:
        boundary = "turn"
    return TargetProbeFailure(
        boundary=boundary,
        message=str(error),
        kind=error.failure.kind,
        operation=operation or None,
    )


def _validation_boundary(error: KensaEvalError) -> Literal["response", "evidence"]:
    cause = error.__cause__
    if isinstance(cause, ValidationError):
        locations = [tuple(str(part) for part in item["loc"]) for item in cause.errors()]
        if any("evidence" in location for location in locations):
            return "evidence"
    return "response"


__all__ = [
    "TargetProbeBoundary",
    "TargetProbeFailure",
    "TargetProbeResult",
    "verify_configured_target",
]
