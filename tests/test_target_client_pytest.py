from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from target_client_support import (
    _assert_serialized_evidence,
    _configure_target,
    _fault_script,
    _host_script,
)

from kensa import pytest_plugin


def test_configured_fixture_matches_in_process_case_results(
    pytester: pytest.Pytester,
) -> None:
    root = Path(str(pytester.path))
    log = root / "target.jsonl"
    script = _host_script(root / "target.py")
    _configure_target(root, (sys.executable, str(script), str(log)))
    pytester.makepyfile(
        test_eval="""
        import pytest
        from kensa.pytest import ConversationResponse, kensa_case


        class Simulator:
            def respond(self, messages):
                return ConversationResponse(content="simulated user")


        @pytest.mark.kensa
        @pytest.mark.parametrize("case", [kensa_case(id="direct", input="hello")])
        def test_direct(case, kensa_run):
            case.run(kensa_run)


        @pytest.mark.kensa
        @pytest.mark.asyncio
        @pytest.mark.parametrize(
            "case",
            [kensa_case(id="simulated", input="hello", termination_reason="done")],
        )
        async def test_simulated(case, kensa_run):
            await case.run(
                kensa_run,
                simulator=Simulator(),
                max_turns=2,
                starts_with="simulator",
            )
        """
    )

    command_run = pytester.runpytest("-q", "--kensa-write-artifacts")

    command_run.assert_outcomes(passed=2)
    command_artifacts = set((root / ".kensa" / "results").glob("*.json"))
    assert len(command_artifacts) == 1
    command_trials = json.loads(next(iter(command_artifacts)).read_text())["trials"]

    pytester.makeconftest(
        """
        import pytest
        from kensa.pytest import ConversationResponse


        @pytest.fixture
        def kensa_run(case):
            class Agent:
                def respond(self, messages):
                    return ConversationResponse(
                        content=f"reply:{len(messages)}",
                        output={"case": case.id, "messages": len(messages)},
                        termination_reason=case.row.get("termination_reason"),
                    )
            return Agent()
        """
    )

    in_process_run = pytester.runpytest("-q", "--kensa-write-artifacts")

    in_process_run.assert_outcomes(passed=2)
    all_artifacts = set((root / ".kensa" / "results").glob("*.json"))
    in_process_artifacts = all_artifacts - command_artifacts
    assert len(in_process_artifacts) == 1
    in_process_trials = json.loads(next(iter(in_process_artifacts)).read_text())["trials"]
    assert len(command_trials) == len(in_process_trials) == 2
    command_results = {trial["case_id"]: trial["output"] for trial in command_trials}
    in_process_results = {trial["case_id"]: trial["output"] for trial in in_process_trials}
    assert command_results == in_process_results


def test_configured_fixture_persists_evidence_in_trial_snapshot(
    pytester: pytest.Pytester,
) -> None:
    root = Path(str(pytester.path))
    log = root / "target.jsonl"
    script = _host_script(root / "target.py")
    _configure_target(root, (sys.executable, str(script), str(log)))
    pytester.makepyfile(
        test_eval="""
        import json
        from pathlib import Path

        import pytest
        from kensa.pytest import kensa_case


        @pytest.mark.kensa
        @pytest.mark.parametrize(
            "case",
            [kensa_case(id="snapshot", input="hello", evidence=True)],
        )
        def test_snapshot(case, kensa_run, kensa_trace):
            case.run(kensa_run)
            assert len(kensa_trace.agent_runs) == 1
            serialized = kensa_trace.agent_runs[0].model_dump(mode="json")
            snapshot_path = next(Path(".kensa/results").glob("*.json"))
            snapshot = json.loads(snapshot_path.read_text())
            assert snapshot["complete"] is False
            assert snapshot["trials"][0]["status"] == "provisional"
            assert snapshot["trials"][0]["trace"]["agent_runs"] == [serialized]
        """
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(passed=1)
    artifact = next((root / ".kensa" / "results").glob("*.json"))
    trial = json.loads(artifact.read_text())["trials"][0]
    _assert_serialized_evidence(
        trial["trace"]["agent_runs"][0],
        case_id="snapshot",
        complete=True,
    )


@pytest.mark.parametrize("xdist", [False, True])
def test_configured_fixture_runs_fresh_processes_and_preserves_evidence(
    pytester: pytest.Pytester,
    xdist: bool,
) -> None:
    root = Path(str(pytester.path))
    log = root / "target.jsonl"
    script = _host_script(root / "target.py")
    _configure_target(root, (sys.executable, str(script), str(log)))
    pytester.makepyfile(
        test_eval="""
        import pytest
        from kensa.pytest import kensa_case


        def assert_evidence(run, case):
            assert run.schema_version == "kensa.agent_run.v1"
            assert run.run_id.endswith(case.id)
            assert run.attestation.revision == "revision-1"
            assert run.attestation.environment == "sandbox"
            assert run.attestation.effects == "sandboxed"
            assert len(run.events) == 1
            assert run.events[0].id == f"event-{run.run_id}"
            assert run.events[0].sequence == 1
            assert run.events[0].kind == "action"
            assert run.events[0].name == "configured-target"
            assert run.events[0].status == "completed"
            assert len(run.state) == 1
            assert run.state[0].name == "session"
            assert run.state[0].value == {"sentinel": run.run_id}
            assert run.state[0].source == "target"
            complete = case.row.get("complete", True)
            assert run.trajectory_completeness == ("complete" if complete else "partial")
            assert run.state_completeness == ("complete" if complete else "unavailable")
            assert run.incomplete_reason == (
                None if complete else "target omitted some evidence"
            )


        @pytest.mark.kensa(trials=2)
        @pytest.mark.parametrize(
            "case",
            [
                kensa_case(id="complete", input="hello", evidence=True),
                kensa_case(
                    id="partial",
                    input="hello",
                    evidence=True,
                    complete=False,
                ),
            ],
        )
        def test_configured(case, kensa_run, kensa_trace):
            result = case.run(kensa_run)
            assert result.output == {"case": case.id, "messages": 0}
            assert result.messages[-1] == {"role": "assistant", "content": "reply:0"}
            assert len(kensa_trace.agent_runs) == 1
            assert_evidence(kensa_trace.agent_runs[0], case)


        class Simulator:
            def respond(self, messages):
                from kensa.pytest import ConversationResponse
                return ConversationResponse(content="simulated user")


        @pytest.mark.kensa
        @pytest.mark.asyncio
        @pytest.mark.parametrize(
            "case",
            [kensa_case(id="simulated", input="hello", termination_reason="done")],
        )
        async def test_simulated(case, kensa_run):
            result = await case.run(
                kensa_run,
                simulator=Simulator(),
                max_turns=2,
                starts_with="simulator",
            )
            assert result.output == {"case": "simulated", "messages": 1}
            assert result.messages == (
                {"role": "user", "content": "simulated user"},
                {"role": "assistant", "content": "reply:1"},
            )
            assert result.termination.source == "agent"
            assert result.termination.reason == "done"
        """
    )

    args = ["-q", "--kensa-write-artifacts"]
    if xdist:
        args.extend(["-n", "2", "--dist=load"])
    result = pytester.runpytest(*args)

    result.assert_outcomes(passed=5)
    events = [json.loads(line) for line in log.read_text().splitlines()]
    opens = [event for event in events if event["event"] == "open"]
    closes = [event for event in events if event["event"] == "close"]
    assert len(opens) == len(closes) == 5
    assert len({event["pid"] for event in opens}) == 5
    assert len({event["sentinel"] for event in opens}) == 5
    assert {event["sentinel"] for event in opens} == {event["sentinel"] for event in closes}
    artifact = next((root / ".kensa" / "results").glob("*.json"))
    trials = json.loads(artifact.read_text())["trials"]
    assert len(trials) == 5
    evidence_trials = [trial for trial in trials if trial["case_id"] != "simulated"]
    assert len({trial["trace"]["agent_runs"][0]["run_id"] for trial in evidence_trials}) == 4
    for trial in evidence_trials:
        _assert_serialized_evidence(
            trial["trace"]["agent_runs"][0],
            case_id=trial["case_id"],
            complete=trial["case_id"] == "complete",
        )
    partial = next(trial for trial in trials if trial["case_id"] == "partial")
    run = partial["trace"]["agent_runs"][0]
    assert run["trajectory_completeness"] == "partial"
    assert run["state_completeness"] == "unavailable"
    assert run["incomplete_reason"] == "target omitted some evidence"
    trace_path = next((root / ".kensa" / "traces" / "runs").glob("*/trials.jsonl"))
    trace_rows = [json.loads(line) for line in trace_path.read_text().splitlines()]
    evidence_trace_rows = [row for row in trace_rows if row["case_id"] != "simulated"]
    assert len(evidence_trace_rows) == 4
    for row in evidence_trace_rows:
        _assert_serialized_evidence(
            row["agent_runs"][0],
            case_id=row["case_id"],
            complete=row["case_id"] == "complete",
        )
        artifact_trial = next(
            trial
            for trial in evidence_trials
            if trial["case_id"] == row["case_id"]
            and trial["trial_index"] == int(row["id"].rsplit("_trial", 1)[1])
        )
        assert row["agent_runs"] == artifact_trial["trace"]["agent_runs"]


@pytest.mark.parametrize(
    ("behavior", "category", "kind"),
    [
        ("turn_error", "agent", "execution"),
        ("crash_turn", "infrastructure", "target_exit"),
    ],
)
def test_configured_fixture_preserves_failure_ownership(
    pytester: pytest.Pytester,
    behavior: str,
    category: str,
    kind: str,
) -> None:
    root = Path(str(pytester.path))
    script = _fault_script(root / "fault.py")
    _configure_target(root, (sys.executable, str(script), behavior), timeout_s=0.2)
    pytester.makepyfile(
        test_eval="""
        import pytest
        from kensa.pytest import kensa_case


        @pytest.mark.kensa
        @pytest.mark.parametrize("case", [kensa_case(id="case", input="hello")])
        def test_failure(case, kensa_run):
            case.run(kensa_run)
        """
    )

    result = pytester.runpytest("-q", "--kensa-write-artifacts")

    result.assert_outcomes(failed=1)
    artifact = next((root / ".kensa" / "results").glob("*.json"))
    failure = json.loads(artifact.read_text())["trials"][0]["failure"]
    assert failure["category"] == category
    assert failure["kind"] == kind
    assert "private diagnostic" not in json.dumps(failure)
    assert "crash diagnostic" not in json.dumps(failure)


def test_repository_fixture_overrides_configured_command(pytester: pytest.Pytester) -> None:
    root = Path(str(pytester.path))
    _configure_target(root, (str(root / "missing-target"),))
    pytester.makeconftest(
        """
        import pytest
        from kensa.pytest import ConversationResponse


        @pytest.fixture
        def kensa_run():
            class Agent:
                def respond(self, messages):
                    return ConversationResponse(output={"source": "repository"})
            return Agent()
        """
    )
    pytester.makepyfile(
        test_eval="""
        import pytest
        from kensa.pytest import kensa_case


        @pytest.mark.kensa
        @pytest.mark.parametrize("case", [kensa_case(id="case", input="hello")])
        def test_override(case, kensa_run):
            assert case.run(kensa_run).output == {"source": "repository"}
        """
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=1)


def test_configured_fixture_resolves_computed_case_fixture(pytester: pytest.Pytester) -> None:
    root = Path(str(pytester.path))
    log = root / "target.jsonl"
    script = _host_script(root / "target.py")
    _configure_target(root, (sys.executable, str(script), str(log)))
    pytester.makeconftest(
        """
        import pytest
        from kensa.pytest import kensa_case


        @pytest.fixture
        def case():
            return kensa_case(id="computed", input="hello")
        """
    )
    pytester.makepyfile(
        test_eval="""
        import pytest


        @pytest.mark.kensa
        def test_computed(case, kensa_run):
            assert case.run(kensa_run).output == {"case": "computed", "messages": 0}
        """
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=1)


def test_configured_fixture_requires_exactly_one_case(pytester: pytest.Pytester) -> None:
    root = Path(str(pytester.path))
    _configure_target(root, (str(root / "unused"),))
    pytester.makepyfile(
        test_eval="""
        import pytest


        @pytest.mark.kensa
        def test_missing_case(kensa_run):
            pass
        """
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*requires exactly one KensaCase fixture value*"])


@pytest.mark.parametrize(
    "source",
    [
        "[tool.kensa]\ntarget_command = []\n",
        "[tool.kensa]\ntarget_timeout_s = 0\n",
    ],
)
def test_invalid_target_configuration_fails_pytest_startup(
    pytester: pytest.Pytester,
    source: str,
) -> None:
    root = Path(str(pytester.path))
    (root / "pyproject.toml").write_text(source)
    pytester.makepyfile("def test_never_runs():\n    pass\n")

    result = pytester.runpytest("-q")

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*invalid Kensa configuration in*pyproject.toml*"])


def test_unrelated_invalid_kensa_configuration_does_not_break_pytest_startup(
    pytester: pytest.Pytester,
) -> None:
    root = Path(str(pytester.path))
    (root / "pyproject.toml").write_text('[tool.kensa]\nevidence_source = "invalid"\n')
    pytester.makepyfile("def test_runs():\n    pass\n")

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=1)


def test_target_timeout_without_command_does_not_register_fixture(
    pytester: pytest.Pytester,
) -> None:
    root = Path(str(pytester.path))
    (root / "pyproject.toml").write_text("[tool.kensa]\ntarget_timeout_s = 1\n")
    pytester.makepyfile("def test_runs():\n    pass\n")

    result = pytester.runpytest("-q")

    result.assert_outcomes(passed=1)


@pytest.mark.parametrize(
    "source",
    [
        "{",
        'tool = "not-a-table"\n',
        '[tool]\nkensa = "not-a-table"\n',
        '[tool.kensa]\nevidence_source = "invalid"\n',
    ],
)
def test_target_configuration_detection_ignores_unrelated_invalid_shapes(
    tmp_path: Path,
    source: str,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(source)

    assert pytest_plugin._declares_target_configuration(pyproject) is False


def test_unconfigured_project_keeps_fixture_not_found_behavior(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_eval="""
        def test_unconfigured(kensa_run):
            pass
        """
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*fixture 'kensa_run' not found*"])
