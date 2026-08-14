from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

from kensa.target_client import TargetCommandSession


def _write_script(path: Path, source: str) -> Path:
    path.write_text(textwrap.dedent(source))
    return path


def _configure_target(root: Path, command: tuple[str, ...], *, timeout_s: float = 2.0) -> None:
    (root / "pyproject.toml").write_text(
        "[tool.kensa]\n"
        f"target_command = {json.dumps(list(command))}\n"
        f"target_timeout_s = {timeout_s}\n"
    )


def _host_script(path: Path) -> Path:
    return _write_script(
        path,
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


        log_path = Path(sys.argv[1])


        class Agent:
            def __init__(self, case):
                self.case = case
                self.sentinel = f"{os.getpid()}-{case.id}"
                with log_path.open("a") as stream:
                    stream.write(json.dumps({
                        "event": "open",
                        "pid": os.getpid(),
                        "case": case.id,
                        "sentinel": self.sentinel,
                    }) + "\\n")

            def respond(self, messages):
                response = ConversationResponse(
                    content=f"reply:{len(messages)}",
                    output={"case": self.case.id, "messages": len(messages)},
                    termination_reason=self.case.row.get("termination_reason"),
                )
                if not self.case.row.get("evidence"):
                    return response
                complete = self.case.row.get("complete", True)
                evidence = AgentRunEvidence(
                    run_id=self.sentinel,
                    attestation=ExecutionAttestation(
                        revision="revision-1",
                        environment="sandbox",
                        effects="sandboxed",
                    ),
                    events=(
                        AgentEvent(
                            id=f"event-{self.sentinel}",
                            sequence=1,
                            kind="action",
                            name="configured-target",
                            status="completed",
                        ),
                    ),
                    trajectory_completeness="complete" if complete else "partial",
                    state=(
                        StateObservation(
                            name="session",
                            value={"sentinel": self.sentinel},
                            source="target",
                        ),
                    ),
                    state_completeness="complete" if complete else "unavailable",
                    incomplete_reason=None if complete else "target omitted some evidence",
                )
                return TargetTurnResult(response=response, evidence=evidence)

            def close(self):
                with log_path.open("a") as stream:
                    stream.write(json.dumps({
                        "event": "close",
                        "pid": os.getpid(),
                        "case": self.case.id,
                        "sentinel": self.sentinel,
                    }) + "\\n")


        raise SystemExit(serve_target(Agent))
        """,
    )


def _fault_script(path: Path) -> Path:
    return _write_script(
        path,
        r"""
        from __future__ import annotations

        import json
        import signal
        import sys
        import time


        behavior = sys.argv[1]


        def read():
            line = sys.stdin.buffer.readline()
            if not line:
                raise SystemExit(0)
            return json.loads(line)


        def write(payload):
            sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
            sys.stdout.flush()


        handshake = read()
        if behavior == "timeout_response":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            sys.stderr.write("target timeout diagnostic\n")
            sys.stderr.flush()
            time.sleep(60)
        if behavior == "crash_handshake":
            sys.stderr.write("target crash diagnostic\n")
            sys.stderr.flush()
            raise SystemExit(7)
        if behavior == "malformed":
            sys.stdout.write("{\n")
            sys.stdout.flush()
            time.sleep(60)
        if behavior == "nan":
            sys.stdout.write(
                '{"type":"handshake","request_id":"%s","version":NaN}\n'
                % handshake["request_id"]
            )
            sys.stdout.flush()
            time.sleep(60)
        if behavior == "non_utf8":
            sys.stdout.buffer.write(b"\xff\n")
            sys.stdout.buffer.flush()
            time.sleep(60)
        if behavior == "no_newline":
            sys.stdout.write("{")
            sys.stdout.flush()
            raise SystemExit(0)
        if behavior == "wrong_request_id":
            write({
                "type": "handshake",
                "request_id": "other",
                "version": "kensa.target.v1",
            })
            time.sleep(60)
        if behavior == "wrong_type":
            write({"type": "shutdown", "request_id": handshake["request_id"]})
            time.sleep(60)
        if behavior == "version_error":
            write({
                "type": "error",
                "request_id": handshake["request_id"],
                "code": "unsupported_version",
                "message": "unsupported",
                "fatal": True,
            })
            time.sleep(60)

        write({
            "type": "handshake",
            "request_id": handshake["request_id"],
            "version": "kensa.target.v1",
        })
        if behavior == "write_timeout":
            time.sleep(60)
        if behavior == "exit_before_open":
            raise SystemExit(7)

        opened = read()
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
            "session_id": "other" if behavior == "session_mismatch" else session_id,
        })
        if behavior == "session_mismatch":
            time.sleep(60)

        turn = read()
        if behavior == "crash_turn":
            sys.stderr.write("turn crash diagnostic\n")
            sys.stderr.flush()
            raise SystemExit(9)
        if behavior.startswith("output_"):
            number = {
                "output_nan": "NaN",
                "output_infinity": "Infinity",
                "output_overflow": "1e400",
            }[behavior]
            sys.stdout.write(
                '{"type":"turn","request_id":"%s","session_id":"%s",'
                '"response":{"content":"reply","output":{"value":%s},'
                '"termination_reason":null}}\n'
                % (turn["request_id"], session_id, number)
            )
            sys.stdout.flush()
            time.sleep(60)
        elif behavior in {"turn_error", "turn_error_large_stderr"}:
            if behavior == "turn_error_large_stderr":
                sys.stderr.write("stderr-head\n" + "x" * 70_000 + "\nstderr-tail\n")
            else:
                sys.stderr.write("responder private diagnostic\n")
            sys.stderr.flush()
            write({
                "type": "error",
                "request_id": turn["request_id"],
                "code": "target_turn_failed",
                "message": "turn failed",
                "fatal": True,
            })
        else:
            write({
                "type": "turn",
                "request_id": turn["request_id"],
                "session_id": session_id,
                "response": {
                    "content": "reply",
                    "output": {"ok": True},
                    "termination_reason": None,
                },
            })

        closed = read()
        if behavior == "cleanup_error":
            sys.stderr.write("cleanup private diagnostic\n")
            sys.stderr.flush()
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
        write({"type": "shutdown", "request_id": shutdown["request_id"]})
        if behavior == "hang_exit":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(60)
        if behavior == "nonzero_exit":
            raise SystemExit(11)
        if behavior == "extra_output":
            time.sleep(0.02)
            write({"type": "shutdown", "request_id": "extra"})
        """,
    )


def _session(script: Path, behavior: str, *, timeout_s: float = 0.5) -> TargetCommandSession:
    return TargetCommandSession(
        (sys.executable, str(script), behavior),
        timeout_s=timeout_s,
        cwd=script.parent,
    )


def _assert_serialized_evidence(
    run: dict[str, Any],
    *,
    case_id: str,
    complete: bool,
) -> None:
    run_id = run["run_id"]
    assert run["schema_version"] == "kensa.agent_run.v1"
    assert run_id.endswith(case_id)
    assert run["attestation"] == {
        "revision": "revision-1",
        "environment": "sandbox",
        "effects": "sandboxed",
    }
    assert run["events"] == [
        {
            "id": f"event-{run_id}",
            "parent_id": None,
            "sequence": 1,
            "kind": "action",
            "name": "configured-target",
            "input": None,
            "output": None,
            "attributes": {},
            "status": "completed",
            "started_at_ns": None,
            "ended_at_ns": None,
        }
    ]
    assert run["state"] == [
        {
            "name": "session",
            "value": {"sentinel": run_id},
            "source": "target",
            "observed_at_ns": None,
        }
    ]
    expected_completeness = "complete" if complete else "partial"
    assert run["trajectory_completeness"] == expected_completeness
    assert run["state_completeness"] == ("complete" if complete else "unavailable")
    assert run["incomplete_reason"] == (None if complete else "target omitted some evidence")
