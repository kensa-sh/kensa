from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from target_probe_support import configure_target, fault_command, success_command, write_case

from kensa import cli, target_probe
from kensa.cli import main
from kensa.errors import KensaEvalError
from kensa.models import KensaProjectConfig


def _stub_doctor_smoke(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> None:
    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["pytest", "tests/evals/test_kensa_smoke.py"],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    monkeypatch.setattr(cli, "_run_persistent_smoke", run)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_configured_target_probe_completes_one_session_and_attestation(
    tmp_path: Path,
    mode: str,
) -> None:
    log = tmp_path / "target.jsonl"
    command = success_command(tmp_path, mode, log)
    case_path = write_case(tmp_path)
    config = KensaProjectConfig(target_command=command, target_timeout_s=0.5)

    result = target_probe.verify_configured_target(
        config,
        case_path=case_path,
        allow_live_effects=False,
        cwd=tmp_path,
    )

    assert result.ready is True
    assert result.failure is None
    assert result.cleanup_failure is None
    assert result.response_non_empty is True
    assert result.observed_lifecycle == [
        "startup",
        "handshake",
        "session_open",
        "turn",
        "response",
        "evidence",
        "effect_policy",
        "cleanup",
    ]
    events = _events(log)
    report = result.to_dict()
    assert report["case_id"] == "readiness"
    assert report["command"] == list(command)
    assert report["attestation"] == {
        "revision": "revision-doctor",
        "environment": "staging",
        "effects": "sandboxed",
    }
    assert report["evidence"] == {
        "run_id": f"{events[0]['pid']}-readiness",
        "trajectory_completeness": "complete",
        "state_completeness": "complete",
        "incomplete_reason": None,
    }
    assert [event["event"] for event in events] == ["open", "turn", "close"]
    assert len({event["pid"] for event in events}) == 1
    assert len({event["sentinel"] for event in events}) == 1


@pytest.mark.parametrize(
    ("behavior", "boundary"),
    [
        ("startup", "startup"),
        ("version", "handshake"),
        ("handshake_only", "session_open"),
        ("open_error", "session_open"),
        ("crash_turn", "turn"),
        ("turn_error", "turn"),
        ("empty", "response"),
        ("empty_output", "response"),
        ("invalid_response", "response"),
        ("missing_attestation", "evidence"),
        ("incomplete_without_reason", "evidence"),
        ("no_evidence", "evidence"),
        ("live", "effect_policy"),
        ("cleanup_error", "cleanup"),
        ("timeout", "timeout"),
    ],
)
@pytest.mark.parametrize("json_output", [False, True])
def test_doctor_reports_target_failure_boundary_in_text_and_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    behavior: str,
    boundary: str,
    json_output: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_doctor_smoke(monkeypatch)
    case_path = write_case(tmp_path)
    if behavior == "startup":
        command = (str(tmp_path / "missing-target"),)
    else:
        command = fault_command(tmp_path, behavior, tmp_path / "target.jsonl")
    configure_target(tmp_path, command, timeout_s=0.05)
    args = ["doctor", "--target-case", str(case_path)]
    if json_output:
        args.append("--json")

    code = main(args)

    captured = capsys.readouterr()
    assert code == 1
    if json_output:
        payload = json.loads(captured.out)
        assert payload["ok"] is False
        assert payload["data"]["target"]["failure"]["boundary"] == boundary
        assert f"failed at {boundary}" in payload["errors"][0]
        assert payload["next_steps"]
    else:
        assert f"failed at {boundary}" in captured.err
        assert "Next steps" in captured.out


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("failure", ["missing_command", "invalid_vector"])
def test_doctor_reports_target_configuration_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    json_output: bool,
    failure: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_doctor_smoke(monkeypatch)
    case_path = write_case(tmp_path)
    if failure == "invalid_vector":
        (tmp_path / "pyproject.toml").write_text("[tool.kensa]\ntarget_command = []\n")
    args = ["doctor", "--target-case", str(case_path)]
    if json_output:
        args.append("--json")

    code = main(args)

    captured = capsys.readouterr()
    assert code == 1
    if json_output:
        payload = json.loads(captured.out)
        target = payload["data"]["target"]
        assert target["attempted"] is False
        assert target["failure"]["boundary"] == "configuration"
        assert payload["errors"][0].startswith(
            "Configured target verification failed at configuration:"
        )
    else:
        assert "failed at configuration" in captured.err


def test_configured_target_without_target_case_keeps_probe_inactive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_doctor_smoke(monkeypatch)
    configure_target(tmp_path, (sys.executable, "unused.py"))

    code = main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["data"]["target"]["requested"] is False
    assert payload["data"]["target"]["configured"] is True
    assert payload["data"]["target"]["failure"] is None
    assert payload["data"]["harness_readiness"] == {
        "ready": True,
        "smoke_eval_count": 1,
    }
    assert not any("--target-case" in error for error in payload["errors"])


def test_invalid_target_config_without_target_case_runs_regular_doctor_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_doctor_smoke(monkeypatch, stdout="real smoke output")
    (tmp_path / "pyproject.toml").write_text("[tool.kensa]\ntarget_timeout_s = 0\n")

    code = main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["data"]["target"]["requested"] is False
    assert payload["data"]["smoke"] == {
        "returncode": 0,
        "stdout": "real smoke output",
        "stderr": "",
    }
    assert any("invalid Kensa configuration" in error for error in payload["errors"])
    assert not any("--target-case" in error for error in payload["errors"])


@pytest.mark.parametrize(
    "source",
    [
        "[]",
        "{}",
        '{"id":"   ","input":"hello"}',
        '{"id":"case","input":NaN}',
        '{"id":"case","input":1e400}',
        '{"id":"case","input":"hello","messages":[]}',
        '{"id":"case","messages":[{"role":"invalid","content":"hello"}]}',
    ],
)
def test_target_probe_rejects_invalid_readiness_cases(tmp_path: Path, source: str) -> None:
    case_path = tmp_path / "case.json"
    case_path.write_text(source)
    config = KensaProjectConfig(
        target_command=(sys.executable, "unused.py"),
        target_timeout_s=0.1,
    )

    result = target_probe.verify_configured_target(
        config,
        case_path=case_path,
        allow_live_effects=False,
        cwd=tmp_path,
    )

    assert result.attempted is False
    assert result.failure is not None
    assert result.failure.boundary == "configuration"
    assert result.failure.kind == "invalid_case"


def test_target_probe_rejects_unreadable_readiness_case(tmp_path: Path) -> None:
    config = KensaProjectConfig(
        target_command=(sys.executable, "unused.py"),
        target_timeout_s=0.1,
    )

    result = target_probe.verify_configured_target(
        config,
        case_path=tmp_path / "missing.json",
        allow_live_effects=False,
        cwd=tmp_path,
    )

    assert result.failure is not None
    assert result.failure.boundary == "configuration"
    assert result.failure.kind == "invalid_case"


def test_doctor_allows_explicit_live_effects_and_states_probe_before_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_doctor_smoke(monkeypatch)
    case_path = write_case(tmp_path)
    command = fault_command(tmp_path, "live", tmp_path / "target.jsonl")
    configure_target(tmp_path, command)
    original = target_probe.verify_configured_target
    invocation_checked = False

    def verify(*args: Any, **kwargs: Any) -> target_probe.TargetProbeResult:
        nonlocal invocation_checked
        warning = " ".join(capsys.readouterr().err.split())
        assert warning == cli._TARGET_PROBE_WARNING
        invocation_checked = True
        return original(*args, **kwargs)

    monkeypatch.setattr(target_probe, "verify_configured_target", verify)

    code = main(
        [
            "doctor",
            "--target-case",
            str(case_path),
            "--allow-live-target-effects",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert invocation_checked is True
    assert code == 0
    assert payload["data"]["target"]["ready"] is True
    assert payload["data"]["target"]["attestation"]["effects"] == "live"


def test_doctor_success_keeps_observation_and_authenticity_claims_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_doctor_smoke(monkeypatch)
    log = tmp_path / "target.jsonl"
    command = success_command(tmp_path, "sync", log)
    configure_target(tmp_path, command)
    case_path = write_case(tmp_path)
    original_read_config = cli.kensa_config.read_project_config
    config_reads = 0

    def read_config(*args: Any, **kwargs: Any) -> KensaProjectConfig:
        nonlocal config_reads
        config_reads += 1
        return original_read_config(*args, **kwargs)

    monkeypatch.setattr(cli.kensa_config, "read_project_config", read_config)

    code = main(["doctor", "--target-case", str(case_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert config_reads == 1
    target = payload["data"]["target"]
    assert target["command"] == list(command)
    assert target["observed_lifecycle"][-1] == "cleanup"
    assert target["attestation"]["revision"] == "revision-doctor"
    rendered = json.dumps(target).lower()
    assert "canonical" not in rendered
    assert "production constructor" not in rendered


def test_doctor_probe_preserves_actual_smoke_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_doctor_smoke(
        monkeypatch,
        returncode=7,
        stdout="smoke stdout",
        stderr="smoke stderr",
    )
    command = success_command(tmp_path, "sync", tmp_path / "target.jsonl")
    configure_target(tmp_path, command)
    case_path = write_case(tmp_path)

    code = main(["doctor", "--target-case", str(case_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["data"]["target"]["ready"] is True
    assert payload["data"]["smoke"] == {
        "returncode": 7,
        "stdout": "smoke stdout",
        "stderr": "smoke stderr",
    }
    assert payload["data"]["harness_readiness"]["smoke_eval_count"] == 0
    assert cli._ENV_SAFETY_WARNING in payload["warnings"]


def test_doctor_probe_cannot_bypass_harness_authenticity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_doctor_smoke(monkeypatch)
    eval_dir = tmp_path / "tests" / "evals"
    eval_dir.mkdir(parents=True)
    (eval_dir / "conftest.py").write_text(
        """import pytest
from kensa.pytest import ConversationResponse


class FakeAgent:
    pass


@pytest.fixture
def kensa_run(case):
    class Agent:
        def respond(self, messages):
            return ConversationResponse(output={"ok": case.input})
    return Agent()
"""
    )
    command = success_command(tmp_path, "sync", tmp_path / "target.jsonl")
    configure_target(tmp_path, command)
    case_path = write_case(tmp_path)

    code = main(["doctor", "--target-case", str(case_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["data"]["target"]["ready"] is True
    assert payload["data"]["harness_readiness"]["smoke_eval_count"] == 1
    assert payload["data"]["harness_authenticity_warnings"]
    assert any("Harness authenticity check failed" in error for error in payload["errors"])


def test_doctor_terminal_reports_config_lifecycle_and_target_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_doctor_smoke(monkeypatch)
    log = tmp_path / "target.jsonl"
    command = success_command(tmp_path, "sync", log)
    configure_target(tmp_path, command)
    case_path = write_case(tmp_path)

    code = main(["doctor", "--target-case", str(case_path)])

    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert code == 0
    assert "Target command:" in rendered
    assert "Observed target lifecycle:" in rendered
    assert "Target-supplied attestation:" in rendered
    assert "effects=sandboxed" in rendered
    assert "Command target readiness: ready" in rendered
    assert "canonical" not in rendered.lower()
    assert "production constructor" not in rendered.lower()


def test_doctor_forwards_complete_message_case_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_doctor_smoke(monkeypatch)
    log = tmp_path / "target.jsonl"
    command = success_command(tmp_path, "sync", log)
    configure_target(tmp_path, command)
    messages = [
        {"role": "system", "content": "verification policy"},
        {"role": "user", "content": "verify readiness"},
    ]
    case_path = write_case(
        tmp_path,
        {"id": "messages-readiness", "messages": messages},
    )

    code = main(["doctor", "--target-case", str(case_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["data"]["target"]["ready"] is True
    events = _events(log)
    assert [event["event"] for event in events] == ["open", "turn", "close"]
    assert events[1]["messages"] == messages
    assert len({event["sentinel"] for event in events}) == 1


def test_verified_target_runs_smoke_and_domain_eval_without_repository_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "target.jsonl"
    command = success_command(tmp_path, "sync", log)
    configure_target(tmp_path, command, timeout_s=1.0)
    case_path = write_case(tmp_path)
    eval_dir = tmp_path / "tests" / "evals"
    eval_dir.mkdir(parents=True)
    (eval_dir / "test_kensa_smoke.py").write_text(
        """import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa
@pytest.mark.parametrize(
    "case",
    [kensa_case(id="kensa_smoke", input="hello")],
)
def test_target_smoke(case, kensa_run):
    result = case.run(kensa_run)
    assert result.output["case"] == "kensa_smoke"
"""
    )
    (eval_dir / "test_domain.py").write_text(
        """import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa
@pytest.mark.parametrize(
    "case",
    [kensa_case(id="domain_case", input="hello")],
)
def test_domain(case, kensa_run):
    result = case.run(kensa_run)
    assert result.output["case"] == "domain_case"
    assert result.messages[-1] == {"role": "assistant", "content": "ready"}
"""
    )

    doctor_code = main(["doctor", "--target-case", str(case_path), "--json"])
    doctor_payload = json.loads(capsys.readouterr().out)
    eval_code = main(["eval", "--workers", "1", "--no-judge", "--json"])
    eval_payload = json.loads(capsys.readouterr().out)

    assert doctor_code == 0
    assert doctor_payload["data"]["target"]["ready"] is True
    assert doctor_payload["data"]["harness_readiness"] == {
        "ready": True,
        "smoke_eval_count": 1,
    }
    assert eval_code == 0
    assert eval_payload["data"]["harness_readiness"]["ready"] is True
    assert eval_payload["data"]["evals_readiness"]["ready"] is True
    assert not (eval_dir / "conftest.py").exists()
    artifact_path = next((tmp_path / ".kensa" / "results").glob("*.json"))
    trials = json.loads(artifact_path.read_text())["trials"]
    assert {trial["case_id"] for trial in trials} == {"kensa_smoke", "domain_case"}
    for trial in trials:
        run = trial["trace"]["agent_runs"][0]
        assert run["attestation"] == {
            "revision": "revision-doctor",
            "environment": "staging",
            "effects": "sandboxed",
        }
        assert run["run_id"].endswith(trial["case_id"])
    events = _events(log)
    assert [event["event"] for event in events] == [
        "open",
        "turn",
        "close",
        "open",
        "turn",
        "close",
        "open",
        "turn",
        "close",
        "open",
        "turn",
        "close",
    ]
    assert len({event["sentinel"] for event in events}) == 4


def test_target_probe_secondary_cleanup_failure_preserves_primary_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_path = write_case(tmp_path)
    config = KensaProjectConfig(
        target_command=fault_command(tmp_path, "no_evidence", tmp_path / "target.jsonl"),
        target_timeout_s=0.1,
    )

    original_close = target_probe.TargetCommandSession.close

    def failed_close(self: target_probe.TargetCommandSession) -> None:
        original_close(self)
        raise KensaEvalError(
            "secondary cleanup failure",
            category="infrastructure",
            kind="target_cleanup",
            evidence={"operation": "shutdown"},
        )

    monkeypatch.setattr(target_probe.TargetCommandSession, "close", failed_close)

    result = target_probe.verify_configured_target(
        config,
        case_path=case_path,
        allow_live_effects=False,
        cwd=tmp_path,
    )

    assert result.failure is not None
    assert result.failure.boundary == "evidence"
    assert result.cleanup_failure is not None
    assert result.cleanup_failure.boundary == "cleanup"


def test_unrequested_target_probe_is_inactive(tmp_path: Path) -> None:
    result = target_probe.verify_configured_target(
        KensaProjectConfig(),
        case_path=None,
        allow_live_effects=False,
        cwd=tmp_path,
    )

    assert result.to_dict() == {
        "requested": False,
        "configured": False,
        "attempted": False,
        "ready": False,
        "command": None,
        "case_path": None,
        "case_id": None,
        "allow_live_effects": False,
        "observed_lifecycle": [],
        "response_non_empty": False,
        "attestation": None,
        "evidence": None,
        "failure": None,
        "cleanup_failure": None,
    }


def test_target_probe_next_steps_cover_every_boundary() -> None:
    boundaries = (
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
    )

    for boundary in boundaries:
        failure = target_probe.TargetProbeFailure(
            boundary=boundary,
            message="failed",
            kind="generic",
        )
        assert cli._target_probe_next_step(failure)


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]
