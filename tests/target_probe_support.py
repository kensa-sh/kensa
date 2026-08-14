from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any


def configure_target(
    root: Path,
    command: tuple[str, ...],
    *,
    timeout_s: float = 0.2,
) -> None:
    (root / "pyproject.toml").write_text(
        "[tool.kensa]\n"
        f"target_command = {json.dumps(list(command))}\n"
        f"target_timeout_s = {timeout_s}\n"
    )


def write_case(root: Path, payload: dict[str, Any] | None = None) -> Path:
    path = root / "readiness.json"
    path.write_text(json.dumps(payload or {"id": "readiness", "input": "hello"}))
    return path


def success_command(root: Path, mode: str, log: Path) -> tuple[str, ...]:
    script = root / "target.py"
    script.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            import json
            import os
            import sys
            from pathlib import Path

            from kensa.conversation import ConversationResponse
            from kensa.target import (
                AgentEvent,
                AgentRunEvidence,
                ExecutionAttestation,
                StateObservation,
            )
            from kensa.target_command import TargetTurnResult, serve_target


            mode = sys.argv[1]
            log_path = Path(sys.argv[2])


            def record(event, sentinel, messages=None):
                with log_path.open("a") as stream:
                    payload = {
                        "event": event,
                        "pid": os.getpid(),
                        "sentinel": sentinel,
                    }
                    if messages is not None:
                        payload["messages"] = messages
                    stream.write(json.dumps(payload) + "\\n")


            def turn_result(case, messages, sentinel):
                evidence = AgentRunEvidence(
                    run_id=sentinel,
                    attestation=ExecutionAttestation(
                        revision="revision-doctor",
                        environment="staging",
                        effects="sandboxed",
                    ),
                    events=(
                        AgentEvent(
                            id=f"event-{sentinel}",
                            sequence=1,
                            kind="action",
                            name="readiness",
                            status="completed",
                        ),
                    ),
                    trajectory_completeness="complete",
                    state=(
                        StateObservation(
                            name="session",
                            value={"sentinel": sentinel},
                            source="target",
                        ),
                    ),
                    state_completeness="complete",
                )
                return TargetTurnResult(
                    response=ConversationResponse(
                        content="ready",
                        output={
                            "case": case.id,
                            "messages": len(messages),
                            "sentinel": sentinel,
                        },
                    ),
                    evidence=evidence,
                )


            class SyncSession:
                def __init__(self, case):
                    self.case = case
                    self.sentinel = f"{os.getpid()}-{case.id}"
                    record("open", self.sentinel)

                def respond(self, messages):
                    record("turn", self.sentinel, messages)
                    return turn_result(self.case, messages, self.sentinel)

                def close(self):
                    record("close", self.sentinel)


            class AsyncSession(SyncSession):
                async def respond(self, messages):
                    record("turn", self.sentinel, messages)
                    return turn_result(self.case, messages, self.sentinel)

                async def close(self):
                    record("close", self.sentinel)


            def open_sync(case):
                return SyncSession(case)


            async def open_async(case):
                return AsyncSession(case)


            raise SystemExit(serve_target(open_async if mode == "async" else open_sync))
            """
        )
    )
    return (sys.executable, str(script), mode, str(log))


def fault_command(root: Path, behavior: str, log: Path) -> tuple[str, ...]:
    script = root / "fault_target.py"
    script.write_text(
        textwrap.dedent(
            r"""
            from __future__ import annotations

            import json
            import os
            import signal
            import sys
            import time
            from pathlib import Path


            behavior = sys.argv[1]
            log_path = Path(sys.argv[2])


            def read():
                line = sys.stdin.readline()
                if not line:
                    raise SystemExit(0)
                return json.loads(line)


            def write(payload):
                sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
                sys.stdout.flush()


            def record(event):
                with log_path.open("a") as stream:
                    stream.write(json.dumps({"event": event, "pid": os.getpid()}) + "\n")


            handshake = read()
            record("handshake")
            if behavior == "timeout":
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                time.sleep(60)
            if behavior == "version":
                write({
                    "type": "handshake",
                    "request_id": handshake["request_id"],
                    "version": "kensa.target.v0",
                })
                time.sleep(60)
            write({
                "type": "handshake",
                "request_id": handshake["request_id"],
                "version": "kensa.target.v1",
            })
            if behavior == "handshake_only":
                raise SystemExit(0)

            opened = read()
            record("open")
            if behavior == "open_error":
                write({
                    "type": "error",
                    "request_id": opened["request_id"],
                    "code": "target_open_failed",
                    "message": "open failed",
                    "fatal": True,
                })
                time.sleep(60)
            session_id = opened["session_id"]
            write({
                "type": "session_opened",
                "request_id": opened["request_id"],
                "session_id": session_id,
            })

            turn = read()
            record("turn")
            if behavior == "crash_turn":
                raise SystemExit(7)
            if behavior == "turn_error":
                write({
                    "type": "error",
                    "request_id": turn["request_id"],
                    "code": "target_turn_failed",
                    "message": "turn failed",
                    "fatal": True,
                })
                time.sleep(60)

            response = {"content": "ready", "output": {"ok": True}}
            evidence = {
                "run_id": "doctor-run",
                "attestation": {
                    "revision": "revision-doctor",
                    "environment": "staging",
                    "effects": "live" if behavior == "live" else "sandboxed",
                },
                "events": [],
                "trajectory_completeness": "complete",
                "state": [],
                "state_completeness": "complete",
            }
            if behavior == "empty":
                response = {}
            if behavior == "empty_output":
                response = {"output": {}}
            if behavior == "invalid_response":
                response = {"content": 42}
            if behavior == "missing_attestation":
                evidence.pop("attestation")
            if behavior == "incomplete_without_reason":
                evidence["trajectory_completeness"] = "partial"
            payload = {
                "type": "turn",
                "request_id": turn["request_id"],
                "session_id": session_id,
                "response": response,
            }
            if behavior != "no_evidence":
                payload["evidence"] = evidence
            write(payload)

            closed = read()
            record("close")
            if behavior == "cleanup_error":
                write({
                    "type": "error",
                    "request_id": closed["request_id"],
                    "code": "target_close_failed",
                    "message": "close failed",
                    "fatal": True,
                })
                time.sleep(60)
            write({
                "type": "session_closed",
                "request_id": closed["request_id"],
                "session_id": session_id,
            })
            shutdown = read()
            record("shutdown")
            write({"type": "shutdown", "request_id": shutdown["request_id"]})
            """
        )
    )
    return (sys.executable, str(script), behavior, str(log))
