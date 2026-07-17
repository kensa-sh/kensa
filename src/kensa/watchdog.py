"""Hard subprocess watchdog for Kensa eval trials."""

from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from kensa.artifacts import (
    load_trials,
    trial_from_dict,
    trial_sort_key,
    upsert_trial,
    write_json_atomic,
    write_run_artifacts,
)
from kensa.runtime import ActiveOperation, TrialMetadata

DEFAULT_JUDGE_TIMEOUT_S = 30.0
DEFAULT_TRIAL_TIMEOUT_S = 300.0
WATCHDOG_HEARTBEAT_INTERVAL_S = 10.0
WATCHDOG_OUTPUT_DRAIN_TIMEOUT_S = 0.5
WATCHDOG_POLL_INTERVAL_S = 0.1
WATCHDOG_TERMINATION_GRACE_S = 3.0
_PROCESS_GROUP_POLL_INTERVAL_S = 0.05
_HEARTBEAT_TEXT_ATTRIBUTES = frozenset({"model", "provider"})
_HEARTBEAT_INTEGER_ATTRIBUTES = frozenset({"attempt", "turn"})
_HEARTBEAT_TEXT_MAX_CHARS = 64
_HEARTBEAT_INPUT_MAX_CHARS = 256
_HEARTBEAT_INTEGER_LIMIT = 1_000_000
_HEARTBEAT_REDACTED = "[REDACTED]"
_HEARTBEAT_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_HEARTBEAT_SECRET = re.compile(
    r"(?:^|[-_.:/])(?:sk|pk|token|secret|api[_-]?key)[-_:][A-Za-z0-9_/-]{8,}",
    re.IGNORECASE,
)
_HEARTBEAT_AWS_KEY = re.compile(r"^AKIA[0-9A-Z]{16}$")
_HEARTBEAT_JWT = re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class ActiveTrial:
    nodeid: str
    group_id: str
    case_id: str
    trial_index: int
    configured_trials: int
    timeout_s: float
    started_monotonic_ns: int
    active_operation: ActiveOperation | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodeid": self.nodeid,
            "group_id": self.group_id,
            "case_id": self.case_id,
            "trial_index": self.trial_index,
            "configured_trials": self.configured_trials,
            "timeout_s": timeout_value(self.timeout_s),
            "started_monotonic_ns": self.started_monotonic_ns,
            "active_operation": (
                self.active_operation.to_dict() if self.active_operation is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ActiveTrial:
        operation_payload = payload.get("active_operation")
        return cls(
            nodeid=str(payload["nodeid"]),
            group_id=str(payload["group_id"]),
            case_id=str(payload["case_id"]),
            trial_index=int(payload["trial_index"]),
            configured_trials=int(payload["configured_trials"]),
            timeout_s=validate_timeout_s(payload["timeout_s"]),
            started_monotonic_ns=int(payload["started_monotonic_ns"]),
            active_operation=(
                ActiveOperation(
                    name=str(operation_payload["name"]),
                    attributes=(
                        operation_payload.get("attributes", {})
                        if isinstance(operation_payload.get("attributes"), dict)
                        else {}
                    ),
                )
                if isinstance(operation_payload, dict)
                else None
            ),
        )


@dataclass(frozen=True)
class WatchdogControl:
    run_id: str
    result_path: Path
    artifact_dir: Path
    default_timeout_s: float
    expected_workers: int | None = None
    judge_timeout_s: float = DEFAULT_JUDGE_TIMEOUT_S
    active_trial: ActiveTrial | None = None
    trial_snapshot: TrialMetadata | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "result_path": str(self.result_path),
            "artifact_dir": str(self.artifact_dir),
            "default_timeout_s": timeout_value(self.default_timeout_s),
            "expected_workers": self.expected_workers,
            "judge_timeout_s": timeout_value(self.judge_timeout_s),
            "active_trial": self.active_trial.to_dict() if self.active_trial else None,
            "trial_snapshot": self.trial_snapshot.to_dict() if self.trial_snapshot else None,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> WatchdogControl:
        active_payload = payload.get("active_trial")
        snapshot_payload = payload.get("trial_snapshot")
        return cls(
            run_id=str(payload["run_id"]),
            result_path=Path(str(payload["result_path"])),
            artifact_dir=Path(str(payload["artifact_dir"])),
            default_timeout_s=validate_timeout_s(payload["default_timeout_s"]),
            expected_workers=(
                int(payload["expected_workers"])
                if payload.get("expected_workers") is not None
                else None
            ),
            judge_timeout_s=validate_timeout_s(
                payload.get("judge_timeout_s", DEFAULT_JUDGE_TIMEOUT_S)
            ),
            active_trial=(
                ActiveTrial.from_dict(active_payload) if isinstance(active_payload, dict) else None
            ),
            trial_snapshot=(
                trial_from_dict(snapshot_payload) if isinstance(snapshot_payload, dict) else None
            ),
        )


@dataclass(frozen=True)
class TimeoutResult:
    case_id: str
    trial_index: int
    timeout_s: float
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "trial_index": self.trial_index,
            "timeout_s": timeout_value(self.timeout_s),
        }


@dataclass(frozen=True)
class EvalProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timeout: TimeoutResult | None = None


def validate_timeout_s(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout must be a positive finite number")
    resolved = float(value)
    if not math.isfinite(resolved) or resolved <= 0:
        raise ValueError("timeout must be a positive finite number")
    return resolved


def timeout_value(value: float) -> int | float:
    return int(value) if int(value) == value else value


def format_timeout_s(value: float) -> str:
    return str(timeout_value(value))


def supported_watchdog_platform() -> bool:
    return os.name == "posix" and (sys.platform == "darwin" or sys.platform.startswith("linux"))


def read_control(path: Path) -> WatchdogControl:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid Kensa watchdog control file: {path}")
    return WatchdogControl.from_dict(payload)


def write_control(path: Path, control: WatchdogControl) -> None:
    write_json_atomic(path, control.to_dict())


def worker_control_path(control_path: Path, worker_id: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9_-]+", worker_id) is None:
        raise ValueError(f"Invalid pytest-xdist worker ID: {worker_id}")
    return control_path.with_name(f"{control_path.stem}-{worker_id}{control_path.suffix}")


def control_paths(control_path: Path) -> list[Path]:
    worker_pattern = f"{control_path.stem}-*{control_path.suffix}"
    paths = [control_path] if control_path.is_file() else []
    return [*paths, *sorted(control_path.parent.glob(worker_pattern))]


def remove_control_files(control_path: Path) -> None:
    for path in control_paths(control_path):
        path.unlink(missing_ok=True)


def run_eval_process(
    cmd: list[str],
    *,
    control_path: Path,
    capture_output: bool,
    heartbeat: Callable[[str], None] | None = None,
) -> EvalProcessResult:
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=capture_output,
        start_new_session=True,
    )
    last_heartbeats: dict[Path, tuple[str, int, int] | None] = {}
    try:
        while True:
            active_controls: list[tuple[Path, WatchdogControl, ActiveTrial]] = []
            observed_controls = _read_controls(control_path)
            for path, control in observed_controls:
                active = control.active_trial
                if active is None:
                    continue
                active_controls.append((path, control, active))
                if heartbeat is not None:
                    last_heartbeats[path] = _emit_heartbeat(
                        active,
                        heartbeat,
                        last_heartbeats.get(path),
                    )
            expired = [entry for entry in active_controls if _trial_expired(entry[2])]
            if expired:
                path, control, active = min(
                    expired,
                    key=lambda entry: (
                        entry[2].started_monotonic_ns + int(entry[2].timeout_s * 1_000_000_000)
                    ),
                )
                try:
                    confirmed_control = read_control(path)
                except FileNotFoundError:
                    confirmed_control = None
                if confirmed_control is not None and confirmed_control.active_trial == active:
                    elapsed_ms = _trial_elapsed_ms(active)
                    _terminate_process_group(process)
                    stdout, stderr = _collect_output(process, capture_output)
                    timeout = _record_timeout(
                        confirmed_control,
                        active,
                        duration_ms=elapsed_ms,
                    )
                    return EvalProcessResult(
                        returncode=1,
                        stdout=stdout,
                        stderr=stderr,
                        timeout=timeout,
                    )
            completed = _wait_once(process, capture_output)
            if completed is not None:
                return completed
    except BaseException:
        _terminate_process_group(process)
        _collect_output(process, capture_output)
        raise


def _read_controls(control_path: Path) -> list[tuple[Path, WatchdogControl]]:
    controls: list[tuple[Path, WatchdogControl]] = []
    for path in control_paths(control_path):
        try:
            controls.append((path, read_control(path)))
        except FileNotFoundError:
            continue
    return controls


def _trial_expired(active: ActiveTrial) -> bool:
    elapsed_ns = time.monotonic_ns() - active.started_monotonic_ns
    return elapsed_ns >= int(active.timeout_s * 1_000_000_000)


def _trial_elapsed_ms(active: ActiveTrial) -> float:
    elapsed_ms = (time.monotonic_ns() - active.started_monotonic_ns) / 1_000_000
    return max(active.timeout_s * 1000, elapsed_ms)


def _emit_heartbeat(
    active: ActiveTrial,
    heartbeat: Callable[[str], None],
    last_heartbeat: tuple[str, int, int] | None,
) -> tuple[str, int, int] | None:
    elapsed_s = max(0.0, (time.monotonic_ns() - active.started_monotonic_ns) / 1_000_000_000)
    interval = int(elapsed_s // WATCHDOG_HEARTBEAT_INTERVAL_S)
    marker = (active.nodeid, active.started_monotonic_ns, interval)
    if interval < 1 or marker == last_heartbeat:
        return last_heartbeat
    heartbeat(format_heartbeat(active, elapsed_s))
    return marker


def format_heartbeat(active: ActiveTrial, elapsed_s: float) -> str:
    parts = [
        f"{_sanitize_heartbeat_text(active.case_id)} trial {active.trial_index}",
        f"{int(elapsed_s)}s",
    ]
    operation = active.active_operation
    if operation is not None:
        parts.append(_sanitize_heartbeat_text(operation.name))
        attributes = _format_heartbeat_attributes(operation.attributes)
        if attributes:
            parts.append(attributes)
    return " | ".join(parts)


def _format_heartbeat_attributes(attributes: dict[str, Any]) -> str:
    rendered: list[str] = []
    safe_keys = _HEARTBEAT_TEXT_ATTRIBUTES | _HEARTBEAT_INTEGER_ATTRIBUTES
    for key in sorted(safe_keys):
        value = attributes.get(key)
        if key in _HEARTBEAT_TEXT_ATTRIBUTES and isinstance(value, str):
            rendered.append(f"{key}={_sanitize_heartbeat_text(value)}")
        elif key in _HEARTBEAT_INTEGER_ATTRIBUTES and type(value) is int:
            rendered_value = (
                str(value) if abs(value) <= _HEARTBEAT_INTEGER_LIMIT else _HEARTBEAT_REDACTED
            )
            rendered.append(f"{key}={rendered_value}")
    return " ".join(rendered)


def _sanitize_heartbeat_text(value: str) -> str:
    if len(value) > _HEARTBEAT_INPUT_MAX_CHARS:
        return _HEARTBEAT_REDACTED
    if not _HEARTBEAT_IDENTIFIER.fullmatch(value):
        return _HEARTBEAT_REDACTED
    if _heartbeat_text_is_secret(value):
        return _HEARTBEAT_REDACTED
    if len(value) > _HEARTBEAT_TEXT_MAX_CHARS:
        return f"{value[: _HEARTBEAT_TEXT_MAX_CHARS - 3]}..."
    return value


def _heartbeat_text_is_secret(value: str) -> bool:
    if _HEARTBEAT_SECRET.search(value) or _HEARTBEAT_AWS_KEY.fullmatch(value):
        return True
    if _HEARTBEAT_JWT.fullmatch(value):
        return True
    return len(value) >= 32 and value.isalnum() and not (value.isalpha() or value.isdigit())


def _wait_once(
    process: subprocess.Popen[str],
    capture_output: bool,
) -> EvalProcessResult | None:
    if capture_output:
        try:
            stdout, stderr = process.communicate(timeout=WATCHDOG_POLL_INTERVAL_S)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                return None
            _terminate_process_group(process)
            stdout, stderr = _collect_output(process, capture_output)
        else:
            _terminate_process_group(process)
    else:
        try:
            process.wait(timeout=WATCHDOG_POLL_INTERVAL_S)
        except subprocess.TimeoutExpired:
            return None
        _terminate_process_group(process)
        stdout, stderr = "", ""
    return EvalProcessResult(
        returncode=int(process.returncode or 0),
        stdout=stdout or "",
        stderr=stderr or "",
    )


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    process_group_id = process.pid
    if not _process_group_exists(process_group_id):
        _reap_process(process)
        return
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        _reap_process(process)
        return

    deadline = time.monotonic() + WATCHDOG_TERMINATION_GRACE_S
    while _process_group_exists(process_group_id):
        process.poll()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_PROCESS_GROUP_POLL_INTERVAL_S, remaining))

    if not _process_group_exists(process_group_id):
        _reap_process(process)
        return
    with suppress(ProcessLookupError):
        os.killpg(process_group_id, signal.SIGKILL)
    _reap_process(process)


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reap_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.wait()


def _collect_output(
    process: subprocess.Popen[str],
    capture_output: bool,
) -> tuple[str, str]:
    if not capture_output:
        return "", ""
    try:
        stdout, stderr = process.communicate(timeout=WATCHDOG_OUTPUT_DRAIN_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                with suppress(OSError):
                    pipe.close()
        stdout, stderr = exc.stdout, exc.stderr
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors="replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    return stdout or "", stderr or ""


def _record_timeout(
    control: WatchdogControl,
    active: ActiveTrial,
    *,
    duration_ms: float,
) -> TimeoutResult:
    message = (
        f"Kensa timeout: {active.case_id} trial {active.trial_index} exceeded "
        f"{format_timeout_s(active.timeout_s)} seconds."
    )
    trials = load_trials(control.result_path)
    existing = next((trial for trial in trials if trial.nodeid == active.nodeid), None)
    if (
        existing is None
        and control.trial_snapshot is not None
        and control.trial_snapshot.nodeid == active.nodeid
    ):
        existing = control.trial_snapshot
    if existing is None:
        trace = {
            "spans": [],
            "tools": [],
            "cost_usd": 0.0,
            "llm_turns": 0,
            "duration_ms": 0.0,
            "incomplete": True,
            "incomplete_reason": message,
        }
        metadata = TrialMetadata(
            nodeid=active.nodeid,
            group_id=active.group_id,
            case_id=active.case_id,
            trial_index=active.trial_index,
            configured_trials=active.configured_trials,
            status="error",
            error=message,
            error_kind="timeout",
            duration_ms=round(duration_ms, 3),
            trace=trace,
            active_operation=(
                active.active_operation.to_dict() if active.active_operation is not None else None
            ),
        )
    else:
        trace = dict(existing.trace)
        trace["incomplete"] = True
        trace["incomplete_reason"] = message
        metadata = replace(
            existing,
            status="error",
            error=message,
            error_kind="timeout",
            duration_ms=round(duration_ms, 3),
            trace=trace,
            active_operation=(
                active.active_operation.to_dict() if active.active_operation is not None else None
            ),
        )
    upsert_trial(trials, metadata)
    trials.sort(key=trial_sort_key)
    write_run_artifacts(
        run_id=control.run_id,
        trials=trials,
        result_path=control.result_path,
        artifact_dir=control.artifact_dir,
        complete=False,
        interruption={
            "kind": "timeout",
            "message": message,
            "nodeid": active.nodeid,
            "case_id": active.case_id,
            "trial_index": active.trial_index,
        },
    )
    return TimeoutResult(
        case_id=active.case_id,
        trial_index=active.trial_index,
        timeout_s=active.timeout_s,
        message=message,
    )


__all__ = [
    "DEFAULT_JUDGE_TIMEOUT_S",
    "DEFAULT_TRIAL_TIMEOUT_S",
    "ActiveTrial",
    "EvalProcessResult",
    "TimeoutResult",
    "WatchdogControl",
    "format_heartbeat",
    "format_timeout_s",
    "read_control",
    "remove_control_files",
    "run_eval_process",
    "supported_watchdog_platform",
    "timeout_value",
    "validate_timeout_s",
    "worker_control_path",
    "write_control",
]
