from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tomllib
import types
from importlib.resources import files as resource_files
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import click
import pytest
from rich.console import Console

from kensa import cli, cli_output, cli_traces
from kensa.cli import main
from kensa.judge import DEFAULT_ANTHROPIC_JUDGE_MODEL
from kensa.llm import DEFAULT_LLM_MODEL
from kensa.traces import ImportResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT

PACKAGED_SKILLS = ("kensa-evals", "kensa-setup", "kensa-inspect", "kensa-generate")


class _FakeLangfuseProviderError(ValueError):
    def __init__(self, message: str, *, label: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.label = label
        self.status_code = status_code


def _read_settings(root: Path) -> dict[str, Any]:
    return json.loads((root / ".kensa" / "settings.json").read_text())


def test_cli_import_does_not_load_langfuse_sdk() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import kensa.cli; "
                "print('kensa.providers.langfuse' in sys.modules); "
                "print('langfuse' in sys.modules)"
            ),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == ["False", "False"]


def test_lazy_langfuse_wrappers_delegate_to_provider_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_provider = types.ModuleType("kensa.providers.langfuse")
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_check(**kwargs: Any) -> None:
        calls.append(("check", kwargs))

    def fake_fetch(**kwargs: Any) -> dict[str, Any]:
        calls.append(("fetch", kwargs))
        return {"data": [], "meta": {}}

    fake_provider_any = cast(Any, fake_provider)
    fake_provider_any.check_langfuse_connection = fake_check
    fake_provider_any.fetch_langfuse_connected_export = fake_fetch
    monkeypatch.setitem(sys.modules, "kensa.providers.langfuse", fake_provider)

    cli.check_langfuse_connection(
        endpoint="https://langfuse.example.com",
        public_key="public",
        secret_key="secret",
    )
    payload = cli.fetch_langfuse_connected_export(
        endpoint="https://langfuse.example.com",
        public_key="public",
        secret_key="secret",
        since="7d",
        limit=5,
        import_mode="observations_v2",
    )

    assert payload == {"data": [], "meta": {}}
    assert calls == [
        (
            "check",
            {
                "endpoint": "https://langfuse.example.com",
                "public_key": "public",
                "secret_key": "secret",
            },
        ),
        (
            "fetch",
            {
                "endpoint": "https://langfuse.example.com",
                "public_key": "public",
                "secret_key": "secret",
                "since": "7d",
                "limit": 5,
                "import_mode": "observations_v2",
            },
        ),
    ]


def _clear_init_credential_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "KENSA_JUDGE_PROVIDER",
        "KENSA_JUDGE_MODEL",
        "KENSA_LLM_PROVIDER",
        "KENSA_LLM_MODEL",
        cli._DOTENV_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)


def _stub_langfuse_auth_check(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[dict[str, Any]] | None = None,
) -> None:
    def fake_auth_check(**kwargs: Any) -> None:
        if calls is not None:
            calls.append(kwargs)

    monkeypatch.setattr(cli, "check_langfuse_connection", fake_auth_check)


def _reject_langfuse_trace_read(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_fetch(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise AssertionError("unexpected trace read")

    monkeypatch.setattr(cli, "fetch_langfuse_connected_export", fail_fetch)


def _write_persistent_smoke(eval_dir: Path, source: str | None = None) -> None:
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / "test_kensa_smoke.py").write_text(
        source
        or """import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="kensa_smoke", input="hello")])
def test_kensa_smoke(case, kensa_run, kensa_trace):
    output = case.run(kensa_run)
    assert output is not None
    assert kensa_trace.llm_turns > 0, (
        "Expected kensa_run to record at least one LLM span. "
        "Wrap the real model/provider call with kensa.record_llm_call(...)."
    )
"""
    )


def _write_ready_harness(eval_dir: Path) -> None:
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / "conftest.py").write_text(
        """import pytest
from kensa import record_llm_call


@pytest.fixture
def kensa_run():
    def _run(case):
        with record_llm_call(provider="test", model="test-model"):
            return {"ok": case.input}

    return _run
"""
    )
    _write_persistent_smoke(eval_dir)


def _write_safe_import_manifest(artifact: Path) -> None:
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "kensa.trace_import_manifest.v1",
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "redaction": {
                    "version": "kensa.redactor.v2",
                    "mandatory": True,
                    "language": "en",
                    "value_redaction_applied": True,
                    "redaction_available": True,
                    "ruleset_hash": cli.redact.RULESET_HASH,
                    "pseudonymization": "instance-counter",
                    "model": {
                        "name": "en_core_web_sm",
                        "version": "3.8.0",
                        "checksum_verified": True,
                    },
                },
            }
        )
    )


def _write_latest_import_pointer(tmp_path: Path, artifact: Path) -> None:
    manifest = artifact.with_suffix(".manifest.json")
    _write_safe_import_manifest(artifact)
    latest = tmp_path / ".kensa" / "traces" / "imports" / "latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(
        json.dumps(
            {
                "schema_version": "kensa.trace_import_latest.v1",
                "provider": "jsonl",
                "source_mode": "file",
                "artifact_path": str(artifact.relative_to(tmp_path)),
                "manifest_path": str(manifest.relative_to(tmp_path)),
                "timestamp": 1792820123,
                "created_at": "2026-06-24T00:00:00Z",
            }
        )
    )


def _trace_view_row(trace_id: str, *, status: str = "ok", span_count: int = 0) -> dict[str, Any]:
    return {
        "schema_version": "kensa.trace_view.v1",
        "id": trace_id,
        "name": trace_id,
        "source": {
            "provider": "jsonl",
            "import_run_id": "import-test",
            "imported_at": "2026-06-24T00:00:00Z",
            "source_path": "traces.jsonl",
            "source_url": None,
            "trace_url": None,
        },
        "started_at_unix_nano": None,
        "ended_at_unix_nano": None,
        "duration_ms": 0.0,
        "status": status,
        "input": None,
        "output": None,
        "attributes": {},
        "spans": [
            {
                "id": f"{trace_id}-span-{index}",
                "trace_id": trace_id,
                "parent_id": None,
                "name": "agent",
                "kind": "span",
                "tool_name": None,
                "started_at_unix_nano": None,
                "ended_at_unix_nano": None,
                "duration_ms": 0.0,
                "status": "ok",
                "status_message": None,
                "input": None,
                "output": None,
                "attributes": {},
                "events": [],
                "raw": None,
            }
            for index in range(span_count)
        ],
        "raw": None,
    }


def _tree_text(root: Path) -> str:
    return "\n".join(path.read_text(errors="ignore") for path in root.rglob("*") if path.is_file())


def test_eval_runs_tests_evals_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    eval_dir = tmp_path / "tests" / "evals"
    eval_dir.mkdir(parents=True)
    (eval_dir / "conftest.py").write_text(
        """import pytest


@pytest.fixture
def kensa_run():
    return lambda case: {"ok": case.input}
"""
    )
    (eval_dir / "test_eval.py").write_text(
        """import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="c1", input="hello")])
def test_agent(case, kensa_run):
    assert case.run(kensa_run) == {"ok": "hello"}
"""
    )

    code = main(["eval"])

    assert code == 0
    assert list((tmp_path / ".kensa" / "results").glob("*.json"))
    trace_files = list((tmp_path / ".kensa" / "traces" / "runs").glob("*/trials.jsonl"))
    assert len(trace_files) == 1
    trace_row = json.loads(trace_files[0].read_text().splitlines()[0])
    assert trace_row["case"] == {"id": "c1", "input": "hello"}
    assert trace_row["output"] == {"ok": "hello"}
    assert "type" not in trace_row


def test_eval_json_fails_when_only_smoke_passes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_ready_harness(tmp_path / "tests" / "evals")

    code = main(["eval", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert payload["data"]["harness_readiness"]["ready"] is True
    assert payload["data"]["harness_readiness"]["smoke_eval_count"] == 1
    assert payload["data"]["evals_readiness"]["ready"] is False
    assert payload["data"]["evals_readiness"]["passing_eval_count"] == 0
    assert "type" not in payload["data"]["aggregates"][0]
    assert payload["data"]["aggregates"][0]["verdict"] == "pass"
    assert any(
        "only the readiness smoke passed" in warning.lower() for warning in payload["warnings"]
    )
    assert any("evals readiness is missing" in error.lower() for error in payload["errors"])
    assert cli._EVALS_NEXT_STEP in payload["next_steps"]


def test_eval_terminal_fails_when_only_smoke_passes_and_reports_trace_path(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_ready_harness(tmp_path / "tests" / "evals")

    code = main(["eval"])

    captured = capsys.readouterr()
    assert code == 1
    assert "only the readiness smoke passed" in captured.out.lower()
    assert ".kensa/traces/runs/" in captured.out
    assert "trials.jsonl" in captured.out
    assert "kensa-evals" in captured.out
    assert "Evals readiness is missing" in captured.err


def test_eval_require_durable_is_removed(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_ready_harness(tmp_path / "tests" / "evals")

    code = main(["eval", "--require-durable", "--json"])

    captured = capsys.readouterr()
    assert code == 2
    assert "No such option" in captured.err
    assert "--require-durable" in captured.err


def test_eval_help_omits_removed_require_durable(capsys) -> None:
    assert main(["eval", "--help"]) == 0
    output = capsys.readouterr().out
    assert "--require-durable" not in output
    assert "Pass extra pytest arguments after --." in output


def test_eval_cli_rejects_bare_pytest_flags_and_accepts_dash_dash(
    monkeypatch,
    capsys,
) -> None:
    calls: list[tuple[list[str], list[str]]] = []

    def fake_cmd_eval(args, pytest_args: list[str]) -> int:
        calls.append((args.paths, pytest_args))
        return 0

    monkeypatch.setattr(cli, "_cmd_eval", fake_cmd_eval)

    assert main(["eval", "-k", "refund"]) == 2
    captured = capsys.readouterr()
    assert "No such option" in captured.err
    assert "-k" in captured.err
    assert calls == []

    assert main(["eval", "--", "-k", "refund", "-q"]) == 0
    assert calls == [(["tests/evals"], ["-k", "refund", "-q"])]


def test_eval_passes_with_domain_eval(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    eval_dir = tmp_path / "tests" / "evals"
    _write_ready_harness(eval_dir)
    (eval_dir / "test_durable.py").write_text(
        """import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="answers_domain_question", input="hello")])
def test_answers_domain_question(case, kensa_run):
    assert case.run(kensa_run) == {"ok": "hello"}
"""
    )

    code = main(["eval", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["data"]["harness_readiness"]["ready"] is True
    assert payload["data"]["harness_readiness"]["smoke_eval_count"] == 1
    assert payload["data"]["evals_readiness"]["ready"] is True
    assert payload["data"]["evals_readiness"]["passing_eval_count"] == 1
    assert not any(
        "only the readiness smoke passed" in warning.lower() for warning in payload["warnings"]
    )


def test_eval_json_reports_no_tests_as_specific_setup_problem(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    eval_dir = tmp_path / "tests" / "evals"
    eval_dir.mkdir(parents=True)
    (eval_dir / "conftest.py").write_text(
        """import pytest


@pytest.fixture
def kensa_run():
    return lambda case: {"ok": case.input}
"""
    )

    code = main(["eval", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 5
    assert payload["summary"] == "Kensa eval found no eval tests."
    assert payload["errors"] == ["No Kensa eval tests were collected from tests/evals."]
    assert payload["next_steps"] == [
        "Run kensa doctor to verify tests/evals/conftest.py::kensa_run(case).",
        cli._EVALS_NEXT_STEP,
    ]
    assert payload["data"]["pytest"]["returncode"] == 5


def test_eval_reports_no_tests_as_specific_terminal_problem(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests" / "evals").mkdir(parents=True)

    code = main(["eval"])

    captured = capsys.readouterr()
    assert code == 5
    assert "Kensa eval found no eval tests" not in captured.out
    assert "No Kensa eval tests were collected from tests/evals." in captured.err
    assert "Next steps" in captured.out
    assert "kensa-evals" in captured.out


def test_eval_terminal_failure_prints_next_steps(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(args=args[0], returncode=1)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    code = cli._cmd_eval(
        SimpleNamespace(
            paths=["tests/evals"],
            json=False,
            json_report=None,
            markdown_report=None,
            no_judge=False,
        ),
        [],
    )

    output = capsys.readouterr().out
    assert code == 1
    assert "Next steps" in output
    assert "Inspect the artifact or report for failing aggregates." in output


def test_doctor_fails_without_persistent_smoke(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)

    code = main(["doctor"])

    assert code == 1
    captured = capsys.readouterr()
    assert "tests/evals/test_kensa_smoke.py" in captured.err
    assert "kensa init" in captured.err
    assert not (tmp_path / "tests" / "evals" / ".kensa_doctor_tmp").exists()
    assert not (tmp_path / ".kensa" / ".doctor_tmp").exists()


def test_doctor_passes_and_records_readiness(
    tmp_path: Path,
    monkeypatch,
    fake_redaction,
) -> None:
    monkeypatch.chdir(tmp_path)
    fake_redaction.make_ready(tmp_path, monkeypatch)
    _write_ready_harness(tmp_path / "tests" / "evals")
    (tmp_path / ".kensa").mkdir(exist_ok=True)
    settings = _read_settings(tmp_path)
    settings["init"] = {"evidence_source": "trace_export"}
    (tmp_path / ".kensa" / "settings.json").write_text(json.dumps(settings))
    (tmp_path / ".kensa" / "readiness.json").write_text("{}")

    code = main(["doctor"])

    assert code == 0
    settings = _read_settings(tmp_path)
    assert settings["schema_version"] == "kensa.settings.v1"
    assert settings["init"]["evidence_source"] == "trace_export"
    assert settings["harness"]["ready"] is True
    assert "checked_at" in settings["harness"]
    assert any("Kensa evals execute" in warning for warning in settings["harness"]["warnings"])
    assert not (tmp_path / ".kensa" / "readiness.json").exists()
    assert (tmp_path / "tests" / "evals" / "test_kensa_smoke.py").exists()
    assert not (tmp_path / "tests" / "evals" / ".kensa_doctor_tmp").exists()


def test_doctor_failure_marks_existing_harness_settings_not_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".kensa").mkdir()
    (tmp_path / ".kensa" / "settings.json").write_text(
        json.dumps(
            {
                "schema_version": "kensa.settings.v1",
                "init": {"evidence_source": "local"},
                "harness": {
                    "ready": True,
                    "checked_at": "2026-07-02T00:00:00Z",
                    "warnings": [],
                },
            }
        )
    )

    assert main(["doctor"]) == 1

    settings = _read_settings(tmp_path)
    assert settings["init"]["evidence_source"] == "local"
    assert settings["harness"]["ready"] is False
    assert settings["harness"]["checked_at"] != "2026-07-02T00:00:00Z"
    assert any(
        "Missing persistent smoke eval" in warning for warning in settings["harness"]["warnings"]
    )


def test_doctor_json_warns_evals_readiness_after_smoke_passes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_ready_harness(tmp_path / "tests" / "evals")

    code = main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["data"]["harness_readiness"]["ready"] is True
    assert payload["data"]["evals_readiness"]["ready"] is False
    assert (
        "Evals readiness also requires one passing domain-shaped Kensa eval." in payload["warnings"]
    )
    assert cli._EVALS_NEXT_STEP in payload["next_steps"]


def test_doctor_terminal_reports_harness_and_evals_readiness(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_ready_harness(tmp_path / "tests" / "evals")

    code = main(["doctor"])

    output = capsys.readouterr().out
    assert code == 0
    assert "Harness readiness: ready" in output
    assert "Evals readiness: missing" in output
    assert "Next steps" in output
    assert cli._EVALS_NEXT_STEP in output

    result_dir = tmp_path / ".kensa" / "results"
    result_dir.mkdir(parents=True)
    (result_dir / "run.json").write_text(
        json.dumps(
            {
                "aggregates": [
                    {
                        "verdict": "pass",
                        "group_id": "tests/evals/test_domain.py::test_domain[case_a]",
                        "case_id": "case_a",
                        "passed": 1,
                        "total": 1,
                    }
                ]
            }
        )
    )

    code = main(["doctor"])

    output = capsys.readouterr().out
    assert code == 0
    assert "Harness readiness: ready" in output
    assert "Evals readiness: ready" in output
    assert "Evals readiness: missing" not in output


def test_doctor_runs_persistent_smoke_file(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    eval_dir = tmp_path / "tests" / "evals"
    _write_ready_harness(eval_dir)
    _write_persistent_smoke(
        eval_dir,
        """def test_kensa_smoke():
    assert False, "persistent smoke executed"
""",
    )

    code = main(["doctor"])

    captured = capsys.readouterr()
    assert code == 1
    assert "persistent smoke executed" in captured.out
    assert not (tmp_path / "tests" / "evals" / ".kensa_doctor_tmp").exists()


def test_doctor_json_reports_missing_persistent_smoke(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    eval_dir = tmp_path / "tests" / "evals"
    eval_dir.mkdir(parents=True)
    (eval_dir / "conftest.py").write_text(
        """import pytest


@pytest.fixture
def kensa_run():
    return lambda case: {"ok": case.input}
"""
    )

    code = main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["data"]["smoke"]["returncode"] == 1
    assert payload["data"]["smoke"]["stdout"] == ""
    assert "tests/evals/test_kensa_smoke.py" in payload["errors"][0]
    assert payload["next_steps"] == [
        "Run kensa init to restore tests/evals/test_kensa_smoke.py, then rerun kensa doctor."
    ]


def test_doctor_does_not_block_on_production_like_env_names(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PRODUCTION_MODE", "true")
    _write_ready_harness(tmp_path / "tests" / "evals")

    code = main(["doctor"])

    assert code == 0
    output = capsys.readouterr().out
    assert "Kensa evals execute" in output
    assert "PRODUCTION_MODE" not in output


def test_doctor_warns_on_non_localhost_network_endpoints(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_API_URL", "https://api.example.com/v1")
    _write_ready_harness(tmp_path / "tests" / "evals")

    code = main(["doctor"])

    assert code == 0
    output = capsys.readouterr().out
    assert "non-local network endpoint" in output
    assert "AGENT_API_URL" in output


def test_doctor_json_emits_agent_envelope(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_ready_harness(tmp_path / "tests" / "evals")

    code = main(["doctor", "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "kensa.cli.v1"
    assert payload["command"] == "doctor"
    assert payload["ok"] is True
    assert payload["data"]["readiness_recorded"] is True
    assert payload["data"]["smoke"]["returncode"] == 0
    assert "stdout" in payload["data"]["smoke"]
    assert "stderr" in payload["data"]["smoke"]
    assert payload["data"]["harness_authenticity_warnings"] == []


def test_doctor_allows_suspicious_harness_with_explicit_opt_out(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    eval_dir = tmp_path / "tests" / "evals"
    eval_dir.mkdir(parents=True)
    (eval_dir / "conftest.py").write_text(
        """import pytest


class _FallbackPrimarySdrAgent:
    pass


@pytest.fixture
def kensa_run():
    return lambda case: {"ok": case.input}
"""
    )
    monkeypatch.setattr(
        cli,
        "_run_persistent_smoke",
        lambda: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )

    code = main(["doctor", "--allow-suspicious-harness"])

    assert code == 0
    output = capsys.readouterr().out
    assert "Harness authenticity warning" in output
    assert "fallback" in output
    assert "mock agent" in output


def test_doctor_fails_on_suspicious_harness_by_default(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    eval_dir = tmp_path / "tests" / "evals"
    eval_dir.mkdir(parents=True)
    (eval_dir / "conftest.py").write_text(
        """import pytest


class FakeAgent:
    pass


@pytest.fixture
def kensa_run():
    return lambda case: {"ok": case.input}
"""
    )
    monkeypatch.setattr(
        cli,
        "_run_persistent_smoke",
        lambda: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )

    code = main(["doctor"])

    captured = capsys.readouterr()
    assert code == 1
    assert "Harness authenticity check failed" in captured.err
    assert not (tmp_path / ".kensa" / "readiness.json").exists()
    assert not (tmp_path / ".kensa" / "settings.json").exists()


def test_run_doctor_check_uses_strict_authenticity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    eval_dir = tmp_path / "tests" / "evals"
    eval_dir.mkdir(parents=True)
    (eval_dir / "conftest.py").write_text(
        """import pytest


class StubAgent:
    pass


@pytest.fixture
def kensa_run():
    return lambda case: {"ok": case.input}
"""
    )
    monkeypatch.setattr(
        cli,
        "_run_persistent_smoke",
        lambda: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )

    result = cli._run_doctor_check()

    assert result.returncode == 1
    assert "Harness authenticity check failed" in result.stderr


def test_doctor_json_reports_suspicious_harness_failure_by_default(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    eval_dir = tmp_path / "tests" / "evals"
    eval_dir.mkdir(parents=True)
    (eval_dir / "conftest.py").write_text(
        """import pytest


class FakeAgent:
    pass


@pytest.fixture
def kensa_run():
    return lambda case: {"ok": case.input}
"""
    )
    monkeypatch.setattr(
        cli,
        "_run_persistent_smoke",
        lambda: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )

    code = main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert payload["data"]["harness_authenticity_warnings"]
    assert "Harness authenticity check failed" in payload["errors"][0]


def test_doctor_fails_on_nested_kensa_workflow_in_subproject(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    (tmp_path / ".git").mkdir()
    backend = tmp_path / "backend"
    eval_dir = backend / "tests" / "evals"
    eval_dir.mkdir(parents=True)
    (backend / ".github" / "workflows").mkdir(parents=True)
    workflow = backend / ".github" / "workflows" / "kensa.yml"
    yaml_workflow = backend / ".github" / "workflows" / "kensa.yaml"
    workflow.write_text("name: stale\n")
    yaml_workflow.write_text("name: stale yaml\n")
    (eval_dir / "conftest.py").write_text(
        """import pytest


@pytest.fixture
def kensa_run():
    return lambda case: {"ok": case.input}
"""
    )
    monkeypatch.setattr(
        cli,
        "_run_persistent_smoke",
        lambda: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )
    monkeypatch.chdir(backend)
    assert set(cli._misplaced_kensa_workflows()) == {workflow, yaml_workflow}

    code = main(["doctor"])

    captured = capsys.readouterr()
    assert code == 1
    assert "GitHub Actions will not run it" in captured.err.replace("\n", "")
    assert ".github/workflows/kensa.yml" in captured.err
    assert ".github/workflows/kensa.yaml" in captured.err


def test_harness_authenticity_detection_covers_known_footguns(monkeypatch) -> None:
    source = """
import pytest

def pytest_addoption(parser):
    parser.addoption("--cov")

class _FallbackPrimarySdrAgent:
    pass

def build():
    try:
        from app import PrimaryAgent
    except ModuleNotFoundError:
        return _FallbackPrimarySdrAgent()
    value = tuple.__new__(tuple)
    return PrimaryAgent.__new__(PrimaryAgent)

def _messages_to_user_texts(value):
    try:
        attach_public_schema()
    except:
        pass
    return ["Can you help me?"]
"""

    warnings = cli._detect_known_suspicious_harness_patterns(source)

    assert len(warnings) == 6
    assert any("fallback" in warning for warning in warnings)
    assert any("__new__" in warning for warning in warnings)
    assert any("ModuleNotFoundError" in warning for warning in warnings)
    assert any("coverage" in warning for warning in warnings)
    assert any("silently swallows" in warning for warning in warnings)
    assert any("hard-coded fallback text" in warning for warning in warnings)
    assert cli._detect_known_suspicious_harness_patterns("not valid python") == []
    assert cli._detect_known_suspicious_harness_patterns("def kensa_run():\n    pass\n") == []
    assert cli._detect_known_suspicious_harness_patterns("class MockAgentClient:\n    pass\n") == []
    assert cli._detect_known_suspicious_harness_patterns("value = tuple.__new__(tuple)\n") == []
    assert (
        cli._detect_known_suspicious_harness_patterns(
            "value = package.TupleFactory.__new__(package.TupleFactory)\n"
        )
        == []
    )
    assert cli._detect_known_suspicious_harness_patterns("value = factory().__new__()\n") == []
    assert cli._detect_known_suspicious_harness_patterns(
        "value = package.PrimaryAgent.__new__(package.PrimaryAgent)\n"
    )
    assert cli._handles_module_not_found(None) is False
    assert cli._handles_module_not_found(ast.Name(id="ValueError")) is False
    assert cli._handles_module_not_found(ast.Constant(value="ModuleNotFoundError")) is False
    assert cli._handles_module_not_found(
        ast.Tuple(elts=[ast.Name(id="ValueError"), ast.Name(id="ModuleNotFoundError")])
    )
    no_fallback_tree = ast.parse(
        "def build():\n"
        "    try:\n"
        "        pass\n"
        "    except ModuleNotFoundError:\n"
        "        return None\n"
    )
    no_fallback_handler = next(
        node for node in ast.walk(no_fallback_tree) if isinstance(node, ast.ExceptHandler)
    )
    assert cli._mentions_replacement_agent(no_fallback_handler) is False
    attr_fallback_tree = ast.parse(
        "def build():\n"
        "    try:\n"
        "        pass\n"
        "    except ModuleNotFoundError:\n"
        "        return adapters.FallbackAgent()\n"
    )
    attr_fallback_handler = next(
        node for node in ast.walk(attr_fallback_tree) if isinstance(node, ast.ExceptHandler)
    )
    assert cli._mentions_replacement_agent(attr_fallback_handler) is True

    class BrokenPath:
        def read_text(self) -> str:
            raise OSError("nope")

    monkeypatch.setattr(cli, "_harness_conftest_path", lambda: BrokenPath())
    assert cli._harness_authenticity_warnings() == [
        "Could not inspect Kensa harness authenticity: nope"
    ]


def test_harness_authenticity_detection_covers_exception_and_input_helpers() -> None:
    def expr(source: str) -> ast.expr:
        return ast.parse(source, mode="eval").body

    assert cli._swallows_broad_exception_silently(
        ast.parse("try:\n    pass\nexcept Exception:\n    pass\n")
    )
    assert cli._swallows_broad_exception_silently(
        ast.parse("try:\n    pass\nexcept builtins.Exception:\n    ...\n")
    )
    assert cli._swallows_broad_exception_silently(
        ast.parse("try:\n    pass\nexcept (ValueError, BaseException):\n    pass\n")
    )
    assert cli._swallows_broad_exception_silently(
        ast.parse("try:\n    pass\nexcept Exception:\n    return None\n")
    )
    assert cli._swallows_broad_exception_silently(
        ast.parse("try:\n    pass\nexcept Exception:\n    return False\n")
    )
    assert cli._swallows_broad_exception_silently(
        ast.parse("while True:\n    try:\n        pass\n    except Exception:\n        continue\n")
    )
    assert not cli._swallows_broad_exception_silently(
        ast.parse("try:\n    pass\nexcept ValueError:\n    raise\n")
    )
    assert not cli._swallows_broad_exception_silently(
        ast.parse("try:\n    pass\nexcept errors.Exception:\n    ...\n")
    )
    assert not cli._swallows_broad_exception_silently(
        ast.parse("try:\n    pass\nexcept Exception:\n    return True\n")
    )
    assert not cli._handles_broad_exception(ast.Constant(value="Exception"))
    assert cli._is_silent_exception_statement(ast.parse("...\n").body[0])
    assert cli._is_silent_exception_statement(ast.parse("return\n").body[0])
    assert cli._is_silent_exception_statement(ast.parse("return False\n").body[0])
    assert not cli._is_silent_exception_statement(ast.parse("raise RuntimeError()\n").body[0])

    assert cli._uses_hardcoded_case_input_fallback(
        ast.parse("def kensa_run(case):\n    return case.row.get('input', 'Can you help?')\n")
    )
    assert cli._uses_hardcoded_case_input_fallback(
        ast.parse("def _run(case):\n    return user_text or 'Fallback prompt'\n")
    )
    assert cli._uses_hardcoded_case_input_fallback(
        ast.parse("def _case_input(row):\n    return row['input'] if row else 'Fallback prompt'\n")
    )
    assert cli._uses_hardcoded_case_input_fallback(
        ast.parse("async def _messages_to_user_texts(value):\n    return ['Fallback prompt']\n")
    )
    assert not cli._uses_hardcoded_case_input_fallback(
        ast.parse("def helper(value):\n    return 'hello'\n")
    )
    assert not cli._uses_hardcoded_case_input_fallback(
        ast.parse("def kensa_run(case):\n    return {'output': 'hello'}\n")
    )
    assert not cli._uses_hardcoded_case_input_fallback(
        ast.parse("def _run(case):\n    return user_text or ''\n")
    )
    assert not cli._uses_hardcoded_case_input_fallback(
        ast.parse("def setup_run_context():\n    return tenant_context or 'default-tenant'\n")
    )
    assert not cli._uses_hardcoded_case_input_fallback(
        ast.parse("def kensa_run(case):\n    return tenant_context or 'default-tenant'\n")
    )

    get_call = expr("row.get('input', 'Fallback prompt')")
    assert isinstance(get_call, ast.Call)
    assert cli._is_get_call_with_hardcoded_text_default(get_call)
    fetch_call = expr("row.fetch('input', 'Fallback prompt')")
    assert isinstance(fetch_call, ast.Call)
    assert not cli._is_get_call_with_hardcoded_text_default(fetch_call)
    safe_get_call = expr("row.get('agent', 'Fallback prompt')")
    assert isinstance(safe_get_call, ast.Call)
    assert not cli._is_get_call_with_hardcoded_text_default(safe_get_call)
    dynamic_get_call = expr("row.get(key, 'Fallback prompt')")
    assert isinstance(dynamic_get_call, ast.Call)
    assert not cli._is_get_call_with_hardcoded_text_default(dynamic_get_call)

    or_expression = expr("user_text or 'Fallback prompt'")
    assert isinstance(or_expression, ast.BoolOp)
    assert cli._is_or_expression_with_hardcoded_text_default(or_expression)
    and_expression = expr("flag and 'Fallback prompt'")
    assert isinstance(and_expression, ast.BoolOp)
    assert not cli._is_or_expression_with_hardcoded_text_default(and_expression)
    context_expression = expr("tenant_context or 'default-tenant'")
    assert isinstance(context_expression, ast.BoolOp)
    assert not cli._is_or_expression_with_hardcoded_text_default(context_expression)

    if_expression = expr("user_input if user_input else 'Fallback prompt'")
    assert isinstance(if_expression, ast.IfExp)
    assert cli._is_if_expression_with_hardcoded_text_default(if_expression)
    non_input_if_expression = expr("value if ok else 'Fallback prompt'")
    assert isinstance(non_input_if_expression, ast.IfExp)
    assert not cli._is_if_expression_with_hardcoded_text_default(non_input_if_expression)
    dynamic_if_expression = expr("user_input if user_input else fallback_prompt")
    assert isinstance(dynamic_if_expression, ast.IfExp)
    assert not cli._is_if_expression_with_hardcoded_text_default(dynamic_if_expression)

    helper_function = ast.parse("def helper(value):\n    return value\n").body[0]
    assert isinstance(helper_function, ast.FunctionDef)
    assert not cli._is_case_adapter_context(helper_function)
    run_context_function = ast.parse("def setup_run_context():\n    return value\n").body[0]
    assert isinstance(run_context_function, ast.FunctionDef)
    assert not cli._is_case_adapter_context(run_context_function)
    run_function = ast.parse("def _run():\n    return value\n").body[0]
    assert isinstance(run_function, ast.FunctionDef)
    assert not cli._is_case_adapter_context(run_function)
    case_arg_function = ast.parse("def helper(case):\n    return case.input\n").body[0]
    assert isinstance(case_arg_function, ast.FunctionDef)
    assert cli._is_case_adapter_context(case_arg_function)
    message_function = ast.parse("def _messages_to_user_texts(value):\n    return value\n").body[0]
    assert isinstance(message_function, ast.FunctionDef)
    assert cli._is_case_adapter_context(message_function)

    assert cli._is_hardcoded_case_input_value(expr("'Fallback prompt'"))
    assert cli._is_hardcoded_case_input_value(expr("('Fallback prompt',)"))
    assert not cli._is_hardcoded_case_input_value(expr("''"))
    assert not cli._is_hardcoded_case_input_value(expr("['hello', 'hi']"))
    assert not cli._is_hardcoded_case_input_value(expr("42"))
    assert cli._is_inputish_expression(expr("user_text"))
    assert cli._is_inputish_expression(expr("case.input"))
    assert cli._is_inputish_expression(expr("row['input']"))
    assert cli._is_inputish_expression(expr("row[user_text]"))
    assert cli._is_inputish_expression(expr("message.strip()"))
    assert cli._is_inputish_expression(expr("user_input or other"))
    assert cli._is_inputish_expression(expr("not user_text"))
    assert cli._is_inputish_expression(expr("user_text == ''"))
    assert not cli._is_inputish_expression(expr("tenant_context"))
    assert not cli._is_inputish_expression(expr("42"))
    assert cli._is_inputish_name("user_text")
    assert cli._is_inputish_name("customer_messages")
    assert not cli._is_inputish_name("tenant_context")
    assert cli._name_tokens("tenant_context") == ("tenant", "context")


def test_eval_json_captures_pytest_output(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    eval_dir = tmp_path / "tests" / "evals"
    eval_dir.mkdir(parents=True)
    (eval_dir / "conftest.py").write_text(
        """import pytest


@pytest.fixture
def kensa_run():
    return lambda case: {"ok": case.input}
"""
    )
    (eval_dir / "test_eval.py").write_text(
        """import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="c1", input="hello")])
def test_agent(case, kensa_run):
    assert case.run(kensa_run) == {"ok": "hello"}
"""
    )

    code = main(["eval", "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "eval"
    assert payload["ok"] is True
    assert payload["data"]["artifact"]
    assert payload["data"]["run_id"]
    assert payload["data"]["aggregates"][0]["verdict"] == "pass"
    assert payload["data"]["pytest"]["returncode"] == 0
    assert "--kensa-report=json" in payload["data"]["pytest_command"]


def test_click_help_version_and_errors_are_handled(monkeypatch, capsys) -> None:
    assert main(["--help"]) == 0
    help_output = capsys.readouterr().out
    assert "░███████" in help_output
    output_lines = help_output.splitlines()
    logo_lines = output_lines[1:8]
    assert logo_lines == cli._cli_logo_with_version().splitlines()
    assert f" v{cli._get_version()} " not in logo_lines[0]
    assert f" v{cli._get_version()} " in logo_lines[-1]
    assert output_lines[8] == ""
    assert "\n  v" not in help_output

    assert main(["--version"]) == 0
    version_output = capsys.readouterr().out
    assert version_output == f"{cli._get_version()}\n"
    assert "░███████" not in version_output

    assert main(["missing"]) == 2
    assert "No such command" in capsys.readouterr().err

    monkeypatch.setattr(
        cli.importlib.metadata,
        "version",
        lambda package: (_ for _ in ()).throw(cli.importlib.metadata.PackageNotFoundError),
    )
    assert cli._get_version() == "dev"

    monkeypatch.setattr(cli, "_get_version", lambda: "x" * 80)
    assert cli._cli_logo_with_version() == cli.CLI_LOGO

    with monkeypatch.context() as context:
        context.setattr(
            cli.cli,
            "main",
            lambda **kwargs: (_ for _ in ()).throw(cli.click.exceptions.Exit(7)),
        )
        assert main(["doctor"]) == 7

    with monkeypatch.context() as context:
        context.setattr(
            cli.cli,
            "main",
            lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
        )
        assert main(["doctor"]) == 130
        assert "Aborted!" in capsys.readouterr().err


def test_help_shows_primary_commands_in_order_and_hides_plumbing(capsys) -> None:
    assert main(["--help"]) == 0

    output = capsys.readouterr().out
    command_positions = [
        output.index(f"  {command}")
        for command in (
            "init",
            "doctor",
            "connect",
            "import",
            "eval",
            "traces",
        )
    ]
    assert command_positions == sorted(command_positions)
    assert "Set up Kensa." in output
    assert "Check your setup." in output
    assert "Connect a trace provider." in output
    assert "Import traces into Kensa through mandatory redaction." in output
    assert "Run your evals." in output
    assert "Trace file access primitives." in output

    assert main(["traces", "--help"]) == 0
    traces_help = capsys.readouterr().out
    assert "list" in traces_help
    assert "sample" in traces_help
    assert "get" in traces_help


def test_readme_cli_quickstart_leads_with_setup_inspect_approve_eval_flow() -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    quickstart = readme.split("### CLI-only", 1)[1].split("## Core commands", 1)[0]
    commands = [
        "kensa init",
        "kensa doctor",
        "kensa import",
        "kensa traces sample",
        "kensa eval",
    ]
    positions = [quickstart.index(command) for command in commands]

    assert positions == sorted(positions)
    assert "kensa-inspect" in quickstart
    assert "approve" in quickstart
    assert "kensa-generate" in quickstart
    assert "`kensa init` asks for the trace source" in readme
    assert "interactive mode" in readme


def test_readme_header_centers_banner_tagline_and_badges() -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    header = readme.split("Generated from traces", 1)[0]
    tagline = '<p align="center">Kensa turns agent traces into pytest evals that run in CI.</p>'
    badge_block = (
        '<p align="center">\n'
        '  <a href="https://github.com/kensa-sh/kensa/actions/workflows/ci.yml">'
    )
    dark_source = (
        '<source media="(prefers-color-scheme: dark)" '
        'srcset="https://raw.githubusercontent.com/kensa-sh/kensa/main/assets/kensa-banner-dark.png">'
    )

    assert '<a href="https://kensa.sh">' in header
    assert dark_source in header
    assert (
        '<img src="https://raw.githubusercontent.com/kensa-sh/kensa/main/assets/'
        'kensa-banner-light.png" width="540" height="auto" alt="Kensa">' in header
    )
    assert header.index('<div align="center">') < header.index(tagline)
    assert header.index(tagline) < header.index(badge_block)
    assert header.index(badge_block) < header.index("<hr />")
    assert (REPO_ROOT / "assets/kensa-banner-dark.png").is_file()
    assert (REPO_ROOT / "assets/kensa-banner-light.png").is_file()


def test_license_names_copyright_holder() -> None:
    license_text = (REPO_ROOT / "LICENSE").read_text()

    assert license_text.startswith("                                 Apache License\n")
    assert "Version 2.0, January 2004" in license_text
    assert "Copyright 2026 Satya Borgohain" in license_text
    assert "Copyright [yyyy] [name of copyright owner]" not in license_text


def test_eval_token_splitting_supports_pytest_passthrough() -> None:
    assert cli._split_eval_tokens(()) == (["tests/evals"], [])
    assert cli._split_eval_tokens(("tests/evals", "-k", "refund", "-q")) == (
        ["tests/evals"],
        ["-k", "refund", "-q"],
    )
    assert cli._split_eval_tokens(("--", "--maxfail=1", "-q")) == (
        ["tests/evals"],
        ["--maxfail=1", "-q"],
    )


def test_top_level_import_writes_timestamped_artifact_manifest_and_latest(
    tmp_path: Path,
    monkeypatch,
    capsys,
    redaction_ready,
) -> None:
    monkeypatch.setattr(cli, "_unix_timestamp", lambda: 1792820123)
    monkeypatch.setattr(cli, "_utc_now_iso", lambda: "2026-06-24T00:00:00Z")
    source = tmp_path / "traces.jsonl"
    source.write_text(json.dumps({"id": "tr_1", "input": "hello"}) + "\n")

    code = main(["import", "--from", "jsonl", "--source", "traces.jsonl", "--json"])

    payload = json.loads(capsys.readouterr().out)
    artifact = tmp_path / ".kensa" / "traces" / "imports" / "jsonl-1792820123.jsonl"
    manifest = tmp_path / ".kensa" / "traces" / "imports" / "jsonl-1792820123.manifest.json"
    latest = tmp_path / ".kensa" / "traces" / "imports" / "latest.json"
    latest_payload = json.loads(latest.read_text())
    assert code == 0
    assert payload["command"] == "import"
    assert artifact.exists()
    assert manifest.exists()
    assert payload["data"]["artifact"] == str(artifact.relative_to(tmp_path))
    assert payload["data"]["manifest"] == str(manifest.relative_to(tmp_path))
    assert latest_payload["provider"] == "jsonl"
    assert latest_payload["source_mode"] == "file"
    assert latest_payload["artifact_path"] == str(artifact.relative_to(tmp_path))
    assert latest_payload["manifest_path"] == str(manifest.relative_to(tmp_path))
    assert latest_payload["timestamp"] == 1792820123
    assert "tr_1" not in latest.read_text()
    assert json.loads(artifact.read_text())["id"] == "tr_1"
    assert json.loads(manifest.read_text())["provider"] == "jsonl"


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--endpoint", "https://collector.example.com"),
        ("--since", "7d"),
    ],
)
def test_top_level_file_import_rejects_connected_options(
    option: str,
    value: str,
    tmp_path: Path,
    capsys,
    redaction_ready,
) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_text(json.dumps({"id": "tr_1", "input": "hello"}) + "\n")

    code = main(
        [
            "import",
            "--from",
            "jsonl",
            "--source",
            str(source.relative_to(tmp_path)),
            option,
            value,
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["errors"] == [
        f"{option} can only be used with connected imports, not with --source."
    ]
    assert not (tmp_path / ".kensa" / "traces" / "imports").exists()


def test_import_project_option_is_removed(capsys) -> None:
    assert main(["import", "--from", "langfuse", "--project", "prod"]) == 2

    captured = capsys.readouterr()
    assert "No such option" in captured.err
    assert "--project" in captured.err


@pytest.mark.parametrize(
    ("provider", "source_name", "source_payload"),
    [
        ("json", "traces.json", {"id": "tr_json", "input": "hello"}),
        (
            "otlp",
            "traces.otlp.json",
            {
                "resourceSpans": [
                    {
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "traceId": "tr_otlp",
                                        "spanId": "span_otlp",
                                        "name": "agent",
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },
        ),
        ("langfuse", "langfuse.json", {"traces": [{"id": "tr_langfuse", "input": "hello"}]}),
    ],
)
def test_top_level_local_file_imports_share_artifact_shape(
    provider: str,
    source_name: str,
    source_payload: dict[str, Any],
    tmp_path: Path,
    monkeypatch,
    capsys,
    redaction_ready,
) -> None:
    monkeypatch.setattr(cli, "_unix_timestamp", lambda: 1792820123)
    source = tmp_path / source_name
    source.write_text(json.dumps(source_payload))

    code = main(["import", "--from", provider, "--source", str(source)])

    output = capsys.readouterr().out
    artifact = tmp_path / ".kensa" / "traces" / "imports" / f"{provider}-1792820123.jsonl"
    manifest = tmp_path / ".kensa" / "traces" / "imports" / f"{provider}-1792820123.manifest.json"
    latest = json.loads((tmp_path / ".kensa" / "traces" / "imports" / "latest.json").read_text())
    assert code == 0
    assert f"provider: {provider}" in output
    assert artifact.exists()
    assert manifest.exists()
    assert latest["provider"] == provider
    assert latest["source_mode"] == "file"
    assert latest["artifact_path"] == str(artifact.relative_to(tmp_path))
    assert latest["manifest_path"] == str(manifest.relative_to(tmp_path))
    assert latest["timestamp"] == 1792820123


def test_connect_commands_write_metadata_without_secret_values(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "lf-public-value")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "lf-secret-value")
    langfuse_checks: list[dict[str, Any]] = []
    _stub_langfuse_auth_check(monkeypatch, langfuse_checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert (
        main(
            [
                "connect",
                "langfuse",
                "--endpoint",
                "https://user:secret@cloud.langfuse.com/api-key",
                "--project",
                "prod",
            ]
        )
        == 0
    )

    capsys.readouterr()
    langfuse = json.loads((tmp_path / ".kensa" / "connections" / "langfuse.json").read_text())
    combined = json.dumps({"langfuse": langfuse})
    assert langfuse["provider"] == "langfuse"
    assert langfuse["endpoint"] == "https://cloud.langfuse.com/[redacted]"
    assert langfuse["auth"] == {
        "type": "basic",
        "public_key_env": "LANGFUSE_PUBLIC_KEY",
        "secret_key_env": "LANGFUSE_SECRET_KEY",
    }
    assert "lf-public-value" not in combined
    assert "lf-secret-value" not in combined
    assert langfuse_checks[0]["public_key"] == "lf-public-value"
    assert langfuse_checks[0]["secret_key"] == "lf-secret-value"


def test_connect_langfuse_does_not_require_trace_read_access(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "lf-public-value")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "lf-secret-value")
    _stub_langfuse_auth_check(monkeypatch)
    _reject_langfuse_trace_read(monkeypatch)

    assert main(["connect", "langfuse", "--endpoint", "https://langfuse.example.com"]) == 0

    capsys.readouterr()
    langfuse = json.loads((tmp_path / ".kensa" / "connections" / "langfuse.json").read_text())
    assert langfuse["endpoint"] == "https://langfuse.example.com"
    assert "lf-secret-value" not in _tree_text(tmp_path / ".kensa")


def test_connect_langfuse_uses_env_base_url_when_endpoint_is_omitted(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "lf-public-value")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "lf-secret-value")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.internal.test")
    langfuse_checks: list[dict[str, Any]] = []
    _stub_langfuse_auth_check(monkeypatch, langfuse_checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert main(["connect", "langfuse"]) == 0

    capsys.readouterr()
    langfuse = json.loads((tmp_path / ".kensa" / "connections" / "langfuse.json").read_text())
    assert langfuse["endpoint"] == "https://langfuse.internal.test"
    assert langfuse_checks[0]["endpoint"] == "https://langfuse.internal.test"


def test_connect_langfuse_uses_configured_dotenv_base_url_when_endpoint_is_omitted(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_init_credential_env(monkeypatch)
    (tmp_path / "pyproject.toml").write_text('[tool.kensa]\ndotenv = "dev.env"\n')
    (tmp_path / "dev.env").write_text(
        "LANGFUSE_PUBLIC_KEY=lf-public-value\n"
        "LANGFUSE_SECRET_KEY=lf-secret-value\n"
        "LANGFUSE_BASE_URL=https://langfuse.internal.test\n"
    )
    langfuse_checks: list[dict[str, Any]] = []
    _stub_langfuse_auth_check(monkeypatch, langfuse_checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert main(["connect", "langfuse"]) == 0

    capsys.readouterr()
    langfuse = json.loads((tmp_path / ".kensa" / "connections" / "langfuse.json").read_text())
    assert langfuse["endpoint"] == "https://langfuse.internal.test"
    assert langfuse_checks[0]["endpoint"] == "https://langfuse.internal.test"


def test_connect_langfuse_rejects_non_url_endpoint(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "lf-public-value")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "lf-secret-value")
    monkeypatch.setattr(
        cli,
        "fetch_langfuse_connected_export",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected fetch")),
    )

    assert main(["connect", "langfuse", "--endpoint", "US"]) == 1

    captured = capsys.readouterr()
    combined = f"{captured.out}\n{captured.err}"
    assert "Langfuse endpoint must be an absolute http(s) URL." in combined
    assert not (tmp_path / ".kensa" / "connections" / "langfuse.json").exists()


def test_connect_langfuse_rejects_empty_explicit_endpoint_without_fallback(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "lf-public-value")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "lf-secret-value")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.internal.test")
    monkeypatch.setattr(
        cli,
        "fetch_langfuse_connected_export",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected fetch")),
    )

    assert main(["connect", "langfuse", "--endpoint", ""]) == 1

    captured = capsys.readouterr()
    combined = f"{captured.out}\n{captured.err}"
    assert "Langfuse endpoint must be an absolute http(s) URL." in combined
    assert not (tmp_path / ".kensa" / "connections" / "langfuse.json").exists()


def test_connect_langfuse_missing_credentials_does_not_write_metadata(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    assert main(["connect", "langfuse"]) == 1

    captured = capsys.readouterr()
    assert "Missing Langfuse credentials" in captured.err
    assert not (tmp_path / ".kensa" / "connections" / "langfuse.json").exists()


def test_connect_langfuse_json_reports_missing_credentials(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    assert main(["connect", "langfuse", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["command"] == "connect langfuse"
    assert "Missing Langfuse credentials" in payload["errors"][0]
    assert not (tmp_path / ".kensa" / "connections" / "langfuse.json").exists()


def test_connect_langfuse_checks_auth_without_trace_read(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "lf-public-value")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "lf-secret-value")
    calls: list[str] = []

    def fake_auth_check(**kwargs: Any) -> None:
        calls.append(f"auth:{kwargs['endpoint']}")

    monkeypatch.setattr(cli, "check_langfuse_connection", fake_auth_check)
    _reject_langfuse_trace_read(monkeypatch)

    assert main(["connect", "langfuse", "--endpoint", "https://langfuse.example.com"]) == 0

    capsys.readouterr()
    assert calls == ["auth:https://langfuse.example.com"]


def test_connect_langfuse_auth_check_failure_does_not_write_metadata(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "lf-public-value")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "lf-secret-value")
    monkeypatch.setattr(
        cli,
        "check_langfuse_connection",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("auth check failed")),
    )
    monkeypatch.setattr(
        cli,
        "fetch_langfuse_connected_export",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected trace read")),
    )

    assert main(["connect", "langfuse", "--endpoint", "https://langfuse.example.com"]) == 1

    captured = capsys.readouterr()
    combined = f"{captured.out}\n{captured.err}"
    assert "auth check failed" in combined
    assert not (tmp_path / ".kensa" / "connections" / "langfuse.json").exists()


def test_connect_langfuse_auth_request_failure_reports_partial_checks(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "lf-public-value")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "lf-secret-value")
    monkeypatch.setattr(
        cli,
        "check_langfuse_connection",
        lambda **kwargs: (_ for _ in ()).throw(
            _FakeLangfuseProviderError("credentials rejected", label="projects", status_code=401)
        ),
    )
    monkeypatch.setattr(
        cli,
        "fetch_langfuse_connected_export",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected trace read")),
    )

    assert main(["connect", "langfuse", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["data"] == {
        "checked": True,
        "checks": {
            "endpoint": True,
            "auth": False,
        },
    }
    assert "credentials rejected" in payload["errors"][0]
    assert not (tmp_path / ".kensa" / "connections" / "langfuse.json").exists()


def test_connect_langfuse_does_not_probe_trace_read_after_auth(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "lf-public-value")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "lf-secret-value")
    _stub_langfuse_auth_check(monkeypatch)
    _reject_langfuse_trace_read(monkeypatch)

    assert main(["connect", "langfuse", "--endpoint", "https://langfuse.example.com"]) == 0

    captured = capsys.readouterr()
    combined = f"{captured.out}\n{captured.err}"
    assert "endpoint reachable: https://langfuse.example.com" in combined
    assert "credentials accepted" in combined
    assert "trace import read access" not in combined
    assert (tmp_path / ".kensa" / "connections" / "langfuse.json").exists()


def test_connect_langfuse_configure_only_skips_checks_and_writes_metadata(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "check_langfuse_connection",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected auth check")),
    )
    monkeypatch.setattr(
        cli,
        "fetch_langfuse_connected_export",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected trace read")),
    )

    assert (
        main(
            [
                "connect",
                "langfuse",
                "--endpoint",
                "https://langfuse.example.com",
                "--configure-only",
            ]
        )
        == 0
    )

    capsys.readouterr()
    connection = json.loads((tmp_path / ".kensa" / "connections" / "langfuse.json").read_text())
    assert connection["endpoint"] == "https://langfuse.example.com"


def test_connect_langfuse_json_reports_check_status_on_success(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "lf-public-value")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "lf-secret-value")
    _stub_langfuse_auth_check(monkeypatch)
    _reject_langfuse_trace_read(monkeypatch)

    assert main(["connect", "langfuse", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["checked"] is True
    assert payload["data"]["checks"] == {
        "endpoint": True,
        "auth": True,
    }


def test_connected_langfuse_import_uses_connection_and_runtime_env(
    tmp_path: Path,
    monkeypatch,
    capsys,
    redaction_ready,
) -> None:
    monkeypatch.setattr(cli, "_unix_timestamp", lambda: 1792820123)
    monkeypatch.setenv("TEST_LANGFUSE_PUBLIC", "lf-public-value")
    monkeypatch.setenv("TEST_LANGFUSE_SECRET", "lf-secret-value")
    _stub_langfuse_auth_check(monkeypatch)
    calls: list[dict[str, Any]] = []
    events: list[str] = []
    redactors: list[cli.redact.Redactor] = []
    create_redactor = cli.redact.Redactor
    write_import = cli._import_trace_records

    def tracking_redactor() -> cli.redact.Redactor:
        events.append("redactor")
        instance = create_redactor()
        redactors.append(instance)
        return instance

    def fake_fetch(**kwargs: Any) -> dict[str, Any]:
        events.append("fetch")
        calls.append(kwargs)
        return {
            "data": [
                {
                    "id": "obs_langfuse",
                    "traceId": "tr_langfuse",
                    "type": "SPAN",
                    "name": "agent",
                    "input": "Refund my charge. Contact Alice with token tok_live",
                }
            ],
            "meta": {"cursor": None},
        }

    def tracking_import(**kwargs: Any) -> ImportResult:
        events.append("import")
        assert kwargs["redactor"] is redactors[-1]
        return write_import(**kwargs)

    monkeypatch.setattr(cli.redact, "Redactor", tracking_redactor)
    monkeypatch.setattr(cli, "fetch_langfuse_connected_export", fake_fetch)
    monkeypatch.setattr(cli, "_import_trace_records", tracking_import)

    assert (
        main(
            [
                "connect",
                "langfuse",
                "--endpoint",
                "https://langfuse.example.com",
                "--public-key-env",
                "TEST_LANGFUSE_PUBLIC",
                "--secret-key-env",
                "TEST_LANGFUSE_SECRET",
            ]
        )
        == 0
    )
    capsys.readouterr()
    calls.clear()

    code = main(["import", "--from", "langfuse", "--since", "7d", "--json"])

    payload = json.loads(capsys.readouterr().out)
    artifact = tmp_path / ".kensa" / "traces" / "imports" / "langfuse-1792820123.jsonl"
    assert code == 0
    assert events == ["redactor", "fetch", "import"]
    assert calls == [
        {
            "endpoint": "https://langfuse.example.com",
            "since": "7d",
            "limit": 50,
            "public_key": "lf-public-value",
            "secret_key": "lf-secret-value",
            "import_mode": "auto",
        }
    ]
    assert payload["data"]["source_mode"] == "connected"
    assert artifact.exists()
    assert "lf-secret-value" not in _tree_text(tmp_path / ".kensa")
    # Raw fetched payloads are redacted in memory and never persisted, in temp
    # files or final artifacts; the sensitive value survives only as an alias.
    assert "tok_live" not in _tree_text(tmp_path)
    assert "[SECRET_" in artifact.read_text()
    assert not list(artifact.parent.glob(".langfuse-connected-*"))
    manifest_payload = json.loads(artifact.with_suffix(".manifest.json").read_text())
    assert manifest_payload["redaction"]["version"] == "kensa.redactor.v2"

    calls.clear()
    events.clear()
    code = main(
        [
            "import",
            "--from",
            "langfuse",
            "--langfuse-mode",
            "observations_v2",
            "--json",
        ]
    )
    json.loads(capsys.readouterr().out)
    assert code == 0
    assert events == ["redactor", "fetch", "import"]
    assert calls[0]["import_mode"] == "observations_v2"


def test_connected_import_checks_redaction_before_fetch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fetched = False

    def fail_redactor() -> None:
        raise cli.redact.RedactionNotReadyError("redaction not ready")

    def track_fetch(**kwargs: Any) -> dict[str, Any]:
        nonlocal fetched
        fetched = True
        return {}

    monkeypatch.setattr(
        cli,
        "_load_connection",
        lambda provider: {"provider": provider, "endpoint": "https://langfuse.example.com"},
    )
    monkeypatch.setattr(cli.redact, "Redactor", fail_redactor)
    monkeypatch.setattr(cli, "_connected_langfuse_payload", track_fetch)

    with pytest.raises(cli.redact.RedactionNotReadyError, match="redaction not ready"):
        cli._connected_import(
            SimpleNamespace(
                provider="langfuse",
                endpoint=None,
                since=None,
                limit=1,
                max_payload_bytes=1_000,
                langfuse_mode="auto",
            ),
            out=tmp_path / "langfuse.jsonl",
        )

    assert fetched is False


def test_new_cli_helper_edge_paths(
    tmp_path: Path,
    monkeypatch,
    capsys,
    fake_redaction,
) -> None:
    monkeypatch.chdir(tmp_path)
    fake_redaction.make_ready(tmp_path, monkeypatch)
    assert isinstance(cli._unix_timestamp(), int)
    with pytest.raises(ValueError, match="unsupported connection provider"):
        cli._connection_metadata(SimpleNamespace(provider="bad", endpoint="x", project=None))

    assert (
        cli._cmd_connect(
            SimpleNamespace(
                provider="langfuse",
                endpoint="https://cloud.langfuse.com",
                project=None,
                public_key_env="LF_PUBLIC",
                secret_key_env="LF_SECRET",
                auth_required=True,
                configure_only=True,
                json=True,
            )
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["command"] == "connect langfuse"

    import_args = SimpleNamespace(
        provider="jsonl",
        source=None,
        endpoint=None,
        since=None,
        limit=1000,
        max_payload_bytes=50_000_000,
        json=False,
    )
    assert cli._cmd_import(import_args) == 2
    assert "pass --source" in capsys.readouterr().err
    import_args.json = True
    assert cli._cmd_import(import_args) == 2
    assert json.loads(capsys.readouterr().out)["exit_code"] == 2

    missing_source_args = SimpleNamespace(**{**import_args.__dict__, "source": "missing.jsonl"})
    missing_source_args.json = True
    assert cli._cmd_import(missing_source_args) == 1
    assert "No such file" in json.loads(capsys.readouterr().out)["errors"][0]
    missing_source_args.json = False
    assert cli._cmd_import(missing_source_args) == 1
    assert "No such file" in capsys.readouterr().err

    secret_source = tmp_path / "secret.jsonl"
    secret_source.write_text(json.dumps({"id": "tr_secret", "api_key": "secret"}) + "\n")
    monkeypatch.setattr(cli, "_unix_timestamp", lambda: 1792820999)
    assert (
        cli._cmd_import(
            SimpleNamespace(
                provider="jsonl",
                source=str(secret_source),
                endpoint=None,
                since=None,
                limit=1000,
                max_payload_bytes=50_000_000,
                json=False,
            )
        )
        == 0
    )
    assert "secret-like fields were redacted" in capsys.readouterr().out

    with pytest.raises(ValueError, match="No missing connection"):
        cli._load_connection("missing")
    (tmp_path / ".kensa" / "connections").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".kensa" / "connections" / "bad.json").write_text(json.dumps({"provider": "other"}))
    with pytest.raises(ValueError, match="Invalid bad connection"):
        cli._load_connection("bad")
    (tmp_path / ".kensa" / "connections" / "bad.json").write_text(
        json.dumps({"provider": "bad", "endpoint": "https://example.com"})
    )
    with pytest.raises(ValueError, match="unsupported connected import provider"):
        cli._connected_import(
            SimpleNamespace(
                provider="bad",
                endpoint=None,
                since=None,
                limit=1,
                max_payload_bytes=1000,
            ),
            out=tmp_path / ".kensa" / "traces" / "imports" / "bad.jsonl",
        )
    (tmp_path / ".kensa" / "connections" / "langfuse.json").write_text(
        json.dumps({"provider": "langfuse", "endpoint": ""})
    )
    with pytest.raises(ValueError, match="missing an endpoint"):
        cli._connected_import(
            SimpleNamespace(
                provider="langfuse",
                endpoint=None,
                since=None,
                limit=1,
                max_payload_bytes=1000,
            ),
            out=tmp_path / ".kensa" / "traces" / "imports" / "langfuse.jsonl",
        )
    with pytest.raises(ValueError, match="Missing Langfuse credentials"):
        cli._connected_langfuse_payload(
            {"auth": {"public_key_env": "MISSING_PUBLIC", "secret_key_env": "MISSING_SECRET"}},
            endpoint="https://langfuse.example.com",
            since=None,
            limit=1,
        )
    with pytest.raises(ValueError, match="unsupported connection provider"):
        cli._check_connection(
            SimpleNamespace(provider="bad"),
            {"provider": "bad", "endpoint": "https://example.com"},
        )

    (tmp_path / ".kensa" / "traces" / "imports" / "latest.json").unlink(missing_ok=True)
    with pytest.raises(ValueError, match="No latest trace import"):
        cli_traces.resolve_trace_view_source(None)
    latest = tmp_path / ".kensa" / "traces" / "imports" / "latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text("[]")
    with pytest.raises(ValueError, match="malformed"):
        cli_traces.resolve_trace_view_source(None)
    latest.write_text("{")
    with pytest.raises(ValueError, match="malformed"):
        cli_traces.resolve_trace_view_source(None)
    latest.write_text(json.dumps({}))
    with pytest.raises(ValueError, match="malformed"):
        cli_traces.resolve_trace_view_source(None)
    latest.write_text(json.dumps({"schema_version": "kensa.trace_import_latest.v1"}))
    with pytest.raises(ValueError, match="missing artifact_path"):
        cli_traces.resolve_trace_view_source(None)
    latest.write_text(
        json.dumps(
            {
                "schema_version": "kensa.trace_import_latest.v1",
                "artifact_path": "missing.jsonl",
            }
        )
    )
    with pytest.raises(ValueError, match="not found"):
        cli_traces.resolve_trace_view_source(None)


def test_eval_json_warns_on_malformed_artifact(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    result_dir = tmp_path / ".kensa" / "results"
    result_dir.mkdir(parents=True)
    (result_dir / "run.json").write_text("{bad")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="pytest out",
            stderr="pytest err",
        ),
    )

    code = main(["eval", "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["data"]["artifact"].endswith("run.json")
    assert payload["data"]["run_id"] is None
    assert payload["data"]["aggregates"] == []
    assert payload["warnings"]
    assert "Could not read eval artifact" in payload["warnings"][0]


def test_eval_json_fails_when_trace_artifact_missing_and_no_durable_evals(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    result_dir = tmp_path / ".kensa" / "results"
    result_dir.mkdir(parents=True)
    (result_dir / "run.json").write_text(json.dumps({"run_id": "run", "aggregates": []}))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="pytest out",
            stderr="pytest err",
        ),
    )

    code = main(["eval", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["data"]["pytest"]["returncode"] == 0
    assert payload["data"]["evals_readiness"]["passing_eval_count"] == 0
    assert any("Trace artifact could not be found" in warning for warning in payload["warnings"])
    assert any("no passing domain evals" in error for error in payload["errors"])


def test_eval_terminal_reports_missing_durable_when_no_aggregates(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    result_dir = tmp_path / ".kensa" / "results"
    result_dir.mkdir(parents=True)
    (result_dir / "run.json").write_text(json.dumps({"run_id": "run", "aggregates": []}))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(args=[], returncode=0),
    )

    code = main(["eval"])

    captured = capsys.readouterr()
    assert code == 1
    assert "Trace artifact could not be found" in captured.out
    normalized_output = " ".join(captured.out.split())
    assert cli._EVALS_NEXT_STEP in normalized_output
    assert "no passing domain evals" in captured.err


def test_init_scaffolds_local_agent_and_ci_files(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda command: "/bin/codex" if command == "codex" else None,
    )

    code = main(["init"])

    assert code == 0
    output = capsys.readouterr().out
    assert "Added files" in output
    assert "✓ .github/workflows/kensa.yml" in output
    assert "✓ tests/evals/conftest.py" in output
    assert "✓ tests/evals/test_kensa_smoke.py" in output
    assert "✓ .kensa/.gitignore" in output
    assert "✓ .kensa/settings.json" in output
    for skill_name in PACKAGED_SKILLS:
        assert f"✓ .agents/skills/{skill_name}/ (1 file)" in output
    assert (tmp_path / ".github" / "workflows" / "kensa.yml").exists()
    settings = _read_settings(tmp_path)
    assert settings["schema_version"] == "kensa.settings.v1"
    assert "evidence_source" not in settings["init"]
    assert settings["init"]["agents"] == ["codex"]
    assert settings["harness"]["ready"] is False
    assert not (tmp_path / ".kensa" / "readiness.json").exists()
    assert (tmp_path / ".kensa" / ".gitignore").read_text() == "*\n!.gitignore\n!settings.json\n"
    for skill_name in PACKAGED_SKILLS:
        assert (tmp_path / ".agents" / "skills" / skill_name / "SKILL.md").exists()
    assert not (tmp_path / ".claude" / "skills" / "kensa-evals" / "SKILL.md").exists()
    assert not (tmp_path / ".cursor" / "skills" / "kensa-evals" / "SKILL.md").exists()
    assert (tmp_path / "tests" / "evals" / "conftest.py").exists()
    assert (tmp_path / "tests" / "evals" / "test_kensa_smoke.py").exists()
    assert not (tmp_path / ".kensa" / "init.md").exists()
    evals_skill = (tmp_path / ".agents" / "skills" / "kensa-evals" / "SKILL.md").read_text()
    setup_skill = (tmp_path / ".agents" / "skills" / "kensa-setup" / "SKILL.md").read_text()
    inspect_skill = (tmp_path / ".agents" / "skills" / "kensa-inspect" / "SKILL.md").read_text()
    generate_skill = (tmp_path / ".agents" / "skills" / "kensa-generate" / "SKILL.md").read_text()
    assert "state-aware Kensa lifecycle" in evals_skill
    assert "setup -> evidence -> inspect -> approval -> generate -> verify" in evals_skill
    assert "Use `kensa-setup`" in evals_skill
    assert "Use `kensa-inspect`" in evals_skill
    assert "Use `kensa-generate`" in evals_skill
    assert "complete when `kensa doctor` passes" in setup_skill
    assert "Do not import traces" in setup_skill
    assert "write pytest eval files" in setup_skill
    assert "kensa import --from" not in setup_skill
    assert "Normally invoked by `kensa-evals`" in setup_skill
    assert ".kensa/inspect/<timestamp>.yaml" in inspect_skill
    assert "status: pending" in inspect_skill
    assert "kensa inspect lint" in inspect_skill
    assert "re-propose" in inspect_skill
    assert "Do not write pytest files" in inspect_skill
    assert "Normally invoked by `kensa-evals`" in inspect_skill
    assert "kensa inspect list --status approved --json" in generate_skill
    assert "status: pending" in generate_skill
    assert "status: rejected" in generate_skill
    assert "status: generated" in generate_skill
    assert "tests/evals/test_*.py" in generate_skill
    assert "explicitly approved by the user" in generate_skill
    conftest = (tmp_path / "tests" / "evals" / "conftest.py").read_text()
    assert "Adapter from a Kensa case" in conftest
    assert "real local agent runtime" in conftest
    smoke = (tmp_path / "tests" / "evals" / "test_kensa_smoke.py").read_text()
    assert "import pytest\n\nfrom kensa.pytest import kensa_case" in smoke
    assert "kensa_case" in smoke
    assert "@pytest.mark.kensa(trials=1)" in smoke
    assert "type=" not in smoke
    assert "case.run(kensa_run)" in smoke
    assert "kensa_trace" in smoke
    assert "assert output is not None" in smoke
    assert "assert kensa_trace.llm_turns > 0" in smoke
    assert "record_llm_call(...)" in smoke
    assert "Copyable setup prompt" in output
    assert "kensa-evals" in output
    assert "Kensa lifecycle" in output


def test_init_settings_write_preserves_harness_section(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".kensa").mkdir()
    (tmp_path / ".kensa" / "settings.json").write_text(
        json.dumps(
            {
                "schema_version": "kensa.settings.v1",
                "init": {"evidence_source": "local"},
                "harness": {
                    "ready": True,
                    "checked_at": "2026-07-02T00:00:00Z",
                    "warnings": ["keep me"],
                },
            }
        )
    )
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)

    assert main(["init"]) == 0

    settings = _read_settings(tmp_path)
    assert settings["init"]["evidence_source"] == "local"
    assert settings["harness"] == {
        "ready": True,
        "checked_at": "2026-07-02T00:00:00Z",
        "warnings": ["keep me"],
    }
    assert not (tmp_path / ".kensa" / "readiness.json").exists()


@pytest.mark.parametrize("source", ["langfuse", "trace_export", "local"])
def test_init_trace_source_flag_installs_redaction_noninteractively(
    source: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
    fake_redaction,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)
    fake_redaction.make_ready(tmp_path, monkeypatch)
    settings_path = tmp_path / ".kensa" / "settings.json"
    settings = json.loads(settings_path.read_text())
    del settings["redaction"]
    settings_path.write_text(json.dumps(settings))
    dependencies_missing = True

    def missing_redaction_dependencies() -> tuple[str, ...]:
        return cli.redact.REDACTION_EXTRA_MODULES if dependencies_missing else ()

    monkeypatch.setattr(
        cli.redact,
        "missing_redaction_dependencies",
        missing_redaction_dependencies,
    )
    install_calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        nonlocal dependencies_missing
        install_calls.append((argv, kwargs))
        dependencies_missing = False
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert main(["init", "--trace-source", source]) == 0

    output = capsys.readouterr().out
    assert "traces will be auto redacted during import" not in output
    assert "redaction dependencies present" not in output
    assert "redaction model ready" not in output
    assert "readiness recorded" not in output
    assert install_calls == [
        (
            [sys.executable, "-m", "pip", "install", "kensa[redaction]"],
            {
                "check": False,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
            },
        )
    ]
    settings = _read_settings(tmp_path)
    assert settings["init"]["evidence_source"] == source
    assert settings["redaction"] == {
        "model": "en_core_web_sm",
        "model_version": "3.8.0",
        "checksum_verified": True,
    }


@pytest.mark.parametrize("source", ["langfuse", "trace_export", "local"])
def test_init_bootstraps_redaction_readiness_when_deps_present(
    source: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
    fake_redaction,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)
    fake_redaction.make_ready(tmp_path, monkeypatch)
    settings_path = tmp_path / ".kensa" / "settings.json"
    settings = json.loads(settings_path.read_text())
    del settings["redaction"]
    settings_path.write_text(json.dumps(settings))

    assert main(["init", "--trace-source", source]) == 0

    output = capsys.readouterr().out
    assert "traces will be auto redacted during import" not in output
    assert "redaction dependencies present" not in output
    assert "redaction model ready" not in output
    assert "readiness recorded" not in output
    settings = _read_settings(tmp_path)
    assert settings["redaction"] == {
        "model": "en_core_web_sm",
        "model_version": "3.8.0",
        "checksum_verified": True,
    }


def test_init_agent_all_flag_scaffolds_all_agent_instructions(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)

    assert main(["init", "--agent", "all", "--trace-source", "local"]) == 0

    output = capsys.readouterr().out
    assert "✓ .agents/skills/kensa-evals/ (1 file)" in output
    assert "✓ .claude/skills/kensa-evals/ (1 file)" in output
    assert "✓ .cursor/skills/kensa-evals/ (1 file)" in output
    for skill_name in PACKAGED_SKILLS:
        assert (tmp_path / ".agents" / "skills" / skill_name / "SKILL.md").exists()
        assert (tmp_path / ".claude" / "skills" / skill_name / "SKILL.md").exists()
        assert (tmp_path / ".cursor" / "skills" / skill_name / "SKILL.md").exists()
    assert _read_settings(tmp_path)["init"] == {
        "evidence_source": "local",
        "agents": ["codex", "claude", "cursor"],
    }


def test_init_explicit_auto_agent_requires_supported_detection(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)

    assert main(["init", "--agent", "auto"]) == 1

    captured = capsys.readouterr()
    assert "Could not detect a supported coding agent for --agent auto" in captured.err
    assert not (tmp_path / "tests" / "evals" / "conftest.py").exists()
    assert not (tmp_path / ".kensa" / "settings.json").exists()


def test_init_agent_none_flag_is_rejected(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["init", "--agent", "none"]) == 2

    captured = capsys.readouterr()
    assert "Invalid value for '--agent'" in captured.err
    assert "'none' is not one of" in captured.err


def test_scaffold_agent_files_explicit_auto_requires_supported_detection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)

    with pytest.raises(click.ClickException, match="Could not detect a supported coding agent"):
        cli._scaffold_agent_files(agent_choice="auto")


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("{", "invalid Kensa settings JSON"),
        ("[]", "Kensa settings must be a JSON object"),
        (
            json.dumps(
                {
                    "schema_version": "kensa.settings.v1",
                    "init": {"evidence_source": "bad"},
                }
            ),
            "invalid Kensa settings",
        ),
        (
            json.dumps({"init": {"evidence_source": "langfuse"}}),
            "invalid Kensa settings",
        ),
        (
            json.dumps(
                {
                    "schema_version": "kensa.settings.v1",
                    "init": {"evidence_source": "langfuse"},
                    "extra": True,
                }
            ),
            "invalid Kensa settings",
        ),
        (
            json.dumps(
                {
                    "schema_version": "kensa.settings.v1",
                    "init": {"evidence_source": "langfuse", "extra": True},
                }
            ),
            "invalid Kensa settings",
        ),
    ],
)
def test_read_settings_rejects_invalid_settings(
    source: str,
    message: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".kensa").mkdir()
    (tmp_path / ".kensa" / "settings.json").write_text(source)

    with pytest.raises(cli.click.ClickException, match=message):
        cli._read_settings()


@pytest.mark.parametrize("command", [["init"], ["doctor"]])
def test_commands_report_invalid_settings_without_traceback(
    command: list[str],
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".kensa").mkdir()
    (tmp_path / ".kensa" / "settings.json").write_text("{")

    assert main(command) == 1

    captured = capsys.readouterr()
    combined_output = captured.out + captured.err
    assert "invalid Kensa settings JSON" in combined_output
    assert "Traceback" not in combined_output


def test_init_overwrites_stale_generated_smoke_and_conftest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)
    eval_dir = tmp_path / "tests" / "evals"
    eval_dir.mkdir(parents=True)
    conftest = eval_dir / "conftest.py"
    smoke = eval_dir / "test_kensa_smoke.py"
    conftest.write_text(
        '''"""Repository-specific Kensa harness connection."""

# Generated by kensa init.

stale = True

def kensa_run():
    def _run(case):
        raise NotImplementedError("Connect this fixture to your agent.")

    return _run
'''
    )
    smoke.write_text(
        """# Generated by kensa init.

def test_kensa_smoke():
    assert False
"""
    )

    code = main(["init"])

    assert code == 0
    assert "stale = True" not in conftest.read_text()
    assert "Adapter from a Kensa case" in conftest.read_text()
    assert "assert False" not in smoke.read_text()
    assert "assert kensa_trace.llm_turns > 0" in smoke.read_text()
    assert main(["init"]) == 0


def test_kensa_skill_templates_are_packaged_and_actionable() -> None:
    template_root = resource_files("kensa").joinpath("skill_templates")
    skill_texts = {
        skill_name: template_root.joinpath(skill_name, "SKILL.md").read_text()
        for skill_name in PACKAGED_SKILLS
    }

    assert "complete when `kensa doctor` passes" in skill_texts["kensa-setup"]
    assert "Do not import traces" in skill_texts["kensa-setup"]
    assert "write pytest eval files" in skill_texts["kensa-setup"]
    assert "Normally invoked by `kensa-evals`" in skill_texts["kensa-setup"]
    assert ".kensa/inspect/<timestamp>.yaml" in skill_texts["kensa-inspect"]
    assert "status: pending" in skill_texts["kensa-inspect"]
    assert "kensa inspect lint" in skill_texts["kensa-inspect"]
    assert "re-propose" in skill_texts["kensa-inspect"]
    assert "Placeholder identities are trace-local" in skill_texts["kensa-inspect"]
    assert "Do not write pytest files" in skill_texts["kensa-inspect"]
    assert "Normally invoked by `kensa-evals`" in skill_texts["kensa-inspect"]
    assert "state-aware Kensa lifecycle" in skill_texts["kensa-evals"]
    assert ".kensa/settings.json" in skill_texts["kensa-evals"]
    assert "`langfuse`, `trace_export`, and `local`" in skill_texts["kensa-evals"]
    assert "source-specific instruction" not in skill_texts["kensa-evals"]
    assert "kensa import --from langfuse\n" in skill_texts["kensa-evals"]
    assert "kensa import --from langfuse --limit" not in skill_texts["kensa-evals"]
    assert "Trace access fails closed" in skill_texts["kensa-evals"]
    assert "Never use raw runtime traces as evidence" in skill_texts["kensa-evals"]
    assert "ask before substantially expanding it" in skill_texts["kensa-evals"]
    assert "0. Detect state" in skill_texts["kensa-evals"]
    assert "1. Setup" in skill_texts["kensa-evals"]
    assert "2. Evidence" in skill_texts["kensa-evals"]
    assert "3. Inspect" in skill_texts["kensa-evals"]
    assert "4. Approval" in skill_texts["kensa-evals"]
    assert "5. Generate" in skill_texts["kensa-evals"]
    assert "6. Verify" in skill_texts["kensa-evals"]
    assert "7. Iterate" in skill_texts["kensa-evals"]
    assert "kensa inspect list --status approved --json" in skill_texts["kensa-generate"]
    assert "status: pending" in skill_texts["kensa-generate"]
    assert "status: rejected" in skill_texts["kensa-generate"]
    assert "status: generated" in skill_texts["kensa-generate"]
    assert "tests/evals/test_*.py" in skill_texts["kensa-generate"]
    assert "Do not import traces" in skill_texts["kensa-generate"]


def test_init_in_subproject_writes_workflow_to_git_root(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    (tmp_path / ".git").mkdir()
    backend = tmp_path / "backend"
    backend.mkdir()
    monkeypatch.chdir(backend)
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)

    code = main(["init"])

    assert code == 0
    output = capsys.readouterr().out
    assert "✓ ../.github/workflows/kensa.yml" in output
    assert "✓ tests/evals/conftest.py" in output
    assert "✓ tests/evals/test_kensa_smoke.py" in output
    assert (tmp_path / ".github" / "workflows" / "kensa.yml").exists()
    assert not (backend / ".github" / "workflows" / "kensa.yml").exists()
    assert (backend / "tests" / "evals" / "conftest.py").exists()
    assert (backend / "tests" / "evals" / "test_kensa_smoke.py").exists()
    workflow = (tmp_path / ".github" / "workflows" / "kensa.yml").read_text()
    assert 'working-directory: "backend"' in workflow
    assert "hashFiles('backend/uv.lock')" in workflow
    assert "if [ -f uv.lock ]; then" in workflow


def test_init_workflow_target_normalizes_symlinked_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    backend = repo / "backend"
    backend.mkdir(parents=True)
    (repo / ".git").mkdir()
    link = tmp_path / "repo-link"
    link.symlink_to(repo, target_is_directory=True)

    assert cli._relative_subproject_path(link / "backend", repo.resolve()) == Path("backend")


def test_subproject_workflow_template_requires_expected_fragments(monkeypatch) -> None:
    monkeypatch.setattr(cli, "WORKFLOW_TEXT", "name: Kensa\n")
    with pytest.raises(RuntimeError, match="top-level steps block"):
        cli._workflow_text(Path("backend"))

    monkeypatch.setattr(cli, "WORKFLOW_TEXT", "name: Kensa\n    steps:\n")
    with pytest.raises(RuntimeError, match="uv lock hashFiles expression"):
        cli._workflow_text(Path("backend"))


@pytest.mark.parametrize(
    ("binary", "expected_skill_root", "unexpected_skill_roots"),
    [
        (
            "codex",
            ".agents/skills",
            (
                ".claude/skills",
                ".cursor/skills",
            ),
        ),
        (
            "claude",
            ".claude/skills",
            (
                ".agents/skills",
                ".cursor/skills",
            ),
        ),
        (
            "cursor-agent",
            ".cursor/skills",
            (
                ".agents/skills",
                ".claude/skills",
            ),
        ),
    ],
)
def test_init_scaffolds_detected_agent_file(
    tmp_path: Path,
    monkeypatch,
    binary: str,
    expected_skill_root: str,
    unexpected_skill_roots: tuple[str, ...],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda command: f"/bin/{command}" if command == binary else None,
    )

    assert main(["init"]) == 0

    for skill_name in PACKAGED_SKILLS:
        assert (tmp_path / expected_skill_root / skill_name / "SKILL.md").exists()
    for unexpected_skill_root in unexpected_skill_roots:
        for skill_name in PACKAGED_SKILLS:
            assert not (tmp_path / unexpected_skill_root / skill_name / "SKILL.md").exists()


def test_init_overwrites_stale_agent_skill_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda command: "/bin/codex" if command == "codex" else None,
    )
    skill_root = tmp_path / ".agents" / "skills" / "kensa-evals"
    references = skill_root / "references"
    references.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("stale skill\n")
    (references / "guardrails.md").write_text("stale guardrails\n")

    assert main(["init"]) == 0

    skill = (skill_root / "SKILL.md").read_text()
    assert "stale skill" not in skill
    assert "state-aware Kensa lifecycle" in skill
    assert not references.exists()
    assert (tmp_path / ".agents" / "skills" / "kensa-setup" / "SKILL.md").exists()
    assert (tmp_path / ".agents" / "skills" / "kensa-inspect" / "SKILL.md").exists()
    generate_skill = (tmp_path / ".agents" / "skills" / "kensa-generate" / "SKILL.md").read_text()
    assert "kensa inspect list --status approved --json" in generate_skill


def test_skill_template_tree_is_packaged_by_build_configuration() -> None:
    template_root = PROJECT_ROOT / "src" / "kensa" / "skill_templates"

    assert sorted(path.name for path in template_root.iterdir() if path.is_dir()) == sorted(
        PACKAGED_SKILLS
    )
    for skill_name in PACKAGED_SKILLS:
        skill_path = template_root / skill_name / "SKILL.md"
        assert skill_path.exists()

    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    build_targets = pyproject["tool"]["hatch"]["build"]["targets"]
    assert "src/kensa" in build_targets["wheel"]["packages"]
    assert "src/kensa" in build_targets["sdist"]["only-include"]


def test_copy_skill_template_tree_requires_skill_md(
    tmp_path: Path,
    monkeypatch,
) -> None:
    template_root = tmp_path / "template"
    (template_root / "kensa-setup").mkdir(parents=True)
    monkeypatch.setattr(cli, "_skill_template_root", lambda: template_root)

    with pytest.raises(RuntimeError, match=r"kensa-evals.*missing SKILL\.md"):
        cli._copy_skill_template_tree(tmp_path / "agent" / "kensa-evals" / "SKILL.md")


def test_copy_skill_template_tree_installs_all_packaged_skills(
    tmp_path: Path,
    monkeypatch,
) -> None:
    template_root = tmp_path / "template"
    for skill_name in PACKAGED_SKILLS:
        skill_dir = template_root / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"{skill_name}\n")
    nested = template_root / "kensa-evals" / "references" / "notes.md"
    nested.parent.mkdir()
    nested.write_text("nested\n")
    empty_dir = template_root / "kensa-evals" / "empty"
    empty_dir.mkdir()
    target = tmp_path / "agent" / "skills" / "kensa-evals" / "SKILL.md"
    monkeypatch.setattr(cli, "_skill_template_root", lambda: template_root)

    assert cli._template_tree_has_files(nested.parent)
    assert not cli._template_tree_has_files(empty_dir)
    assert not cli._template_tree_has_files(template_root / "missing")

    written = cli._copy_skill_template_tree(target)

    assert written == [
        tmp_path / "agent" / "skills" / "kensa-evals" / "SKILL.md",
        tmp_path / "agent" / "skills" / "kensa-evals" / "references" / "notes.md",
        tmp_path / "agent" / "skills" / "kensa-setup" / "SKILL.md",
        tmp_path / "agent" / "skills" / "kensa-inspect" / "SKILL.md",
        tmp_path / "agent" / "skills" / "kensa-generate" / "SKILL.md",
    ]
    for skill_name in PACKAGED_SKILLS:
        assert (
            tmp_path / "agent" / "skills" / skill_name / "SKILL.md"
        ).read_text() == f"{skill_name}\n"
    assert (
        tmp_path / "agent" / "skills" / "kensa-evals" / "references" / "notes.md"
    ).read_text() == "nested\n"


def test_copy_skill_template_files_recurses_into_directories(tmp_path: Path) -> None:
    source = tmp_path / "source"
    nested = source / "references"
    nested.mkdir(parents=True)
    (source / "SKILL.md").write_text("skill\n")
    (nested / "guide.md").write_text("guide\n")

    written = cli._copy_skill_template_files(source, tmp_path / "destination")

    assert written == [
        tmp_path / "destination" / "SKILL.md",
        tmp_path / "destination" / "references" / "guide.md",
    ]
    assert (tmp_path / "destination" / "references" / "guide.md").read_text() == "guide\n"


def test_copy_skill_template_tree_replaces_stale_skill_directories(
    tmp_path: Path,
    monkeypatch,
) -> None:
    template_root = tmp_path / "template"
    for skill_name in PACKAGED_SKILLS:
        skill_dir = template_root / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"{skill_name}\n")
    stale = tmp_path / "agent" / "skills" / "kensa-evals" / "references" / "old.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("old\n")
    note = tmp_path / "agent" / "skills" / "kensa-evals" / "local-note.md"
    note.write_text("keep\n")
    monkeypatch.setattr(cli, "_skill_template_root", lambda: template_root)

    cli._copy_skill_template_tree(tmp_path / "agent" / "skills" / "kensa-evals" / "SKILL.md")

    assert not stale.exists()
    assert note.read_text() == "keep\n"
    assert (tmp_path / "agent" / "skills" / "kensa-evals" / "SKILL.md").read_text() == (
        "kensa-evals\n"
    )


def test_skill_template_root_fails_loudly_when_packaging_omits_tree(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_SKILL_TEMPLATE_ROOT", ("missing-skill-template",))

    with pytest.raises(RuntimeError, match="kensa/missing-skill-template"):
        cli._skill_template_root()


def test_init_interactive_agent_choice_scaffolds_selected_file(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)
    monkeypatch.setattr(cli, "_configure_trace_source_connection", lambda steps, source: "ready")
    monkeypatch.setattr(cli, "_configure_redaction_readiness", lambda steps, source: "ready")
    keys = iter(["\r", "\r"])
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))

    assert main(["init"]) == 0

    output = capsys.readouterr().out
    assert "Which coding agent" in output
    assert "░███████" in output
    assert "┌  kensa init" in output
    assert f"Kensa will install {len(cli._PACKAGED_SKILLS)} skill files" in output
    assert "installed Claude Code instructions" in output
    assert "◇  Created" in output
    for skill_name in PACKAGED_SKILLS:
        assert f"✓ .claude/skills/{skill_name}/ (1 file)" in output
    assert "◇  Finish setup" in output
    assert "Where should Kensa get traces from?" in output
    assert "Note: traces will be auto redacted during import." in output
    assert "Existing trace export" in output
    assert "Langfuse" in output
    assert "No traces? Capture local run" in output
    assert "Next steps:" in output
    assert "1. Open Claude Code." in output
    assert (
        '2. Paste this: "Use /kensa-evals to continue the Kensa lifecycle for this repo."' in output
    )
    assert "kensa import --from <provider> --source <file>" not in output
    assert "kensa eval" not in output
    assert "Fastest path:" not in output
    assert "Prefer to do it yourself?" not in output
    assert "Copyable setup prompt" not in output
    assert "└  Setup files ready" in output
    assert _read_settings(tmp_path)["init"]["evidence_source"] == "langfuse"
    for skill_name in PACKAGED_SKILLS:
        assert (tmp_path / ".claude" / "skills" / skill_name / "SKILL.md").exists()
        assert not (tmp_path / ".agents" / "skills" / skill_name / "SKILL.md").exists()
        assert not (tmp_path / ".cursor" / "skills" / skill_name / "SKILL.md").exists()


def test_init_interactive_agent_choice_other_installs_agents_tree(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli.shutil, "which", lambda command: "/bin/codex")
    monkeypatch.setattr(cli, "_configure_trace_source_connection", lambda steps, source: "ready")
    monkeypatch.setattr(cli, "_configure_redaction_readiness", lambda steps, source: "ready")
    keys = iter(["j", "j", "j", "\r", "\r"])
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))

    assert main(["init"]) == 0

    output = capsys.readouterr().out
    assert "installed Other instructions" in output
    assert _read_settings(tmp_path)["init"]["evidence_source"] == "langfuse"
    for skill_name in PACKAGED_SKILLS:
        assert (tmp_path / ".agents" / "skills" / skill_name / "SKILL.md").exists()
        assert not (tmp_path / ".claude" / "skills" / skill_name / "SKILL.md").exists()
        assert not (tmp_path / ".cursor" / "skills" / skill_name / "SKILL.md").exists()


def test_init_interactive_keyboard_menu_defaults_to_first_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)
    monkeypatch.setattr(cli, "_configure_trace_source_connection", lambda steps, source: "ready")
    monkeypatch.setattr(cli, "_configure_redaction_readiness", lambda steps, source: "ready")
    keys = iter(["x", "\r", "\r"])
    console = Console(record=True, highlight=False)
    monkeypatch.setattr(cli_output, "CONSOLE", console)
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))

    assert main(["init"]) == 0

    output = console.export_text()
    assert "Which coding agent?" in output
    assert "Use ↑/↓, then Enter." in output
    assert "installed Claude Code instructions" in output
    assert "Where should Kensa get traces from?" in output
    assert "Note: traces will be auto redacted during import." in output
    assert "Next steps:" in output
    assert "1. Open Claude Code." in output
    assert (
        '2. Paste this: "Use /kensa-evals to continue the Kensa lifecycle for this repo."' in output
    )
    assert "Copyable setup prompt" not in output
    assert "Prefer to do it yourself?" not in output
    assert _read_settings(tmp_path)["init"]["evidence_source"] == "langfuse"
    for skill_name in PACKAGED_SKILLS:
        assert (tmp_path / ".claude" / "skills" / skill_name / "SKILL.md").exists()


def test_init_explicit_auto_agent_summary_uses_resolved_agent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda command: "/bin/codex" if command == "codex" else None,
    )
    monkeypatch.setattr(cli, "_configure_trace_source_connection", lambda steps, source: "ready")
    monkeypatch.setattr(cli, "_configure_redaction_readiness", lambda steps, source: "ready")
    keys = iter(["\r"])
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))

    assert main(["init", "--agent", "auto"]) == 0

    output = capsys.readouterr().out
    assert "installed Codex instructions" in output
    assert "installed auto instructions" not in output
    assert _read_settings(tmp_path)["init"]["agents"] == ["codex"]


def test_select_agent_instruction_interactive_without_steps_uses_label_menu(
    monkeypatch,
) -> None:
    console = Console(record=True, highlight=False)
    keys = iter(["j", "j", "\r"])
    monkeypatch.setattr(cli_output, "CONSOLE", console)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))

    choice, targets = cli._select_agent_instruction()

    assert choice == "cursor"
    assert targets == (("cursor", Path(".cursor/skills/kensa-evals/SKILL.md")),)
    output = console.export_text()
    assert "Claude Code" in output
    assert "Codex" in output
    assert "Cursor" in output
    assert "Other" in output
    assert "All supported" not in output
    assert "None" not in output


def test_select_interactive_choice_wraps_up_from_first_option(monkeypatch) -> None:
    console = Console(record=True, highlight=False)
    keys = iter(["k", "\r"])
    monkeypatch.setattr(cli_output, "CONSOLE", console)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))

    choice = cli._select_interactive_choice(
        "Which coding agent?",
        choices=cli._AGENT_INSTRUCTION_CHOICES,
        default=cli._AGENT_INSTRUCTION_CHOICES[0],
    )

    assert choice == "other"


def test_select_trace_source_interactive_without_steps_uses_label_menu(monkeypatch) -> None:
    console = Console(record=True, highlight=False)
    keys = iter(["j", "\r"])
    monkeypatch.setattr(cli_output, "CONSOLE", console)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))

    assert cli._select_trace_source() == "trace_export"
    assert cli._TRACE_SOURCE_CHOICES == (
        "langfuse",
        "trace_export",
        "local",
    )
    output = console.export_text()
    assert "Langfuse" in output
    assert "Existing trace export" in output
    assert "No traces? Capture local run" in output
    assert "trace_export" not in output


def test_select_trace_source_noninteractive_has_no_default(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)

    assert cli._select_trace_source() is None


def test_select_interactive_choice_without_tty_uses_prompt(monkeypatch) -> None:
    console = Console(record=True, highlight=False)
    prompt_calls: list[tuple[str, dict[str, object]]] = []

    def fake_prompt(text: str, **kwargs: object) -> str:
        prompt_calls.append((text, kwargs))
        return "cursor"

    monkeypatch.setattr(cli_output, "CONSOLE", console)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    monkeypatch.setattr(cli.click, "prompt", fake_prompt)

    choice = cli._select_interactive_choice(
        "Which coding agent?",
        choices=cli._AGENT_INSTRUCTION_CHOICES,
        default="codex",
    )

    assert choice == "cursor"
    assert prompt_calls
    assert prompt_calls[0][0] == "Which coding agent?"
    assert prompt_calls[0][1]["default"] == "codex"
    assert f"Kensa will install {len(cli._PACKAGED_SKILLS)} skill files" in console.export_text()


def test_init_langfuse_credentials_create_root_dotenv_and_connect(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    keys = iter(["\r", "\r", "\r"])
    prompts = iter(["pk-test", "sk-test", "openai-test"])
    checks: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    monkeypatch.setattr(cli.click, "prompt", lambda *args, **kwargs: next(prompts))
    _stub_langfuse_auth_check(monkeypatch, checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert cli._configure_trace_source_connection(None, "langfuse") == "ready"

    output = capsys.readouterr().out
    dotenv = tmp_path / ".env.local"
    connection = json.loads((tmp_path / ".kensa" / "connections" / "langfuse.json").read_text())
    assert "LANGFUSE_PUBLIC_KEY='pk-test'" in dotenv.read_text()
    assert "LANGFUSE_SECRET_KEY='sk-test'" in dotenv.read_text()
    assert "OPENAI_API_KEY='openai-test'" in dotenv.read_text()
    assert "KENSA_JUDGE_PROVIDER='openai'" in dotenv.read_text()
    assert f"KENSA_JUDGE_MODEL='{DEFAULT_LLM_MODEL}'" in dotenv.read_text()
    assert "LANGFUSE_BASE_URL='https://cloud.langfuse.com'" in dotenv.read_text()
    assert (tmp_path / ".gitignore").read_text() == ".env.local\n"
    assert tomllib.loads((tmp_path / "pyproject.toml").read_text())["tool"]["kensa"] == {
        "dotenv": ".env.local"
    }
    assert connection["auth"]["public_key_env"] == "LANGFUSE_PUBLIC_KEY"
    assert "pk-test" not in json.dumps(connection)
    assert "sk-test" not in json.dumps(connection)
    assert "openai-test" not in json.dumps(connection)
    assert checks[0]["public_key"] == "pk-test"
    assert checks[0]["secret_key"] == "sk-test"
    assert "pk-test" not in output
    assert "sk-test" not in output
    assert "openai-test" not in output


def test_init_langfuse_credentials_create_warns_for_unsupported_judge_provider(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    monkeypatch.setenv("KENSA_JUDGE_PROVIDER", "gemini")
    keys = iter(["\r", "\r", "\r"])
    prompts = iter(["pk-test", "sk-test", "openai-test"])
    checks: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    monkeypatch.setattr(cli.click, "prompt", lambda *args, **kwargs: next(prompts))
    _stub_langfuse_auth_check(monkeypatch, checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert cli._configure_trace_source_connection(None, "langfuse") == "ready"

    output = capsys.readouterr().out
    normalized = " ".join(output.split())
    dotenv = (tmp_path / ".env.local").read_text()
    assert "Unsupported KENSA_JUDGE_PROVIDER: gemini" in output
    assert "built-in judge supports openai and anthropic" in normalized
    assert "set_judge_provider()" in normalized
    assert "KENSA_JUDGE_PROVIDER='openai'" in dotenv
    assert "OPENAI_API_KEY='openai-test'" in dotenv
    assert checks[0]["public_key"] == "pk-test"


def test_init_langfuse_credentials_create_can_skip_judge_key(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    keys = iter(["\r", "\r", "\r"])
    prompts = iter(["pk-test", "sk-test", ""])
    checks: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    monkeypatch.setattr(cli.click, "prompt", lambda *args, **kwargs: next(prompts))
    _stub_langfuse_auth_check(monkeypatch, checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert cli._configure_trace_source_connection(None, "langfuse") == "ready"

    output = capsys.readouterr().out
    dotenv = tmp_path / ".env.local"
    dotenv_text = dotenv.read_text()
    assert "LANGFUSE_PUBLIC_KEY='pk-test'" in dotenv_text
    assert "LANGFUSE_SECRET_KEY='sk-test'" in dotenv_text
    assert "OPENAI_API_KEY" not in dotenv_text
    assert "KENSA_JUDGE_PROVIDER" not in dotenv_text
    assert "KENSA_JUDGE_MODEL" not in dotenv_text
    assert checks[0]["public_key"] == "pk-test"
    assert "LLM-as-judge key not found" in output
    assert "OPENAI_API_KEY or ANTHROPIC_API_KEY" in output
    assert "pk-test" not in output
    assert "sk-test" not in output


def test_init_langfuse_credentials_can_create_anthropic_judge_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    keys = iter(["\r", "j", "\r", "\r"])
    prompts = iter(["pk-test", "sk-test", "anthropic-test"])
    checks: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    monkeypatch.setattr(cli.click, "prompt", lambda *args, **kwargs: next(prompts))
    _stub_langfuse_auth_check(monkeypatch, checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert cli._configure_trace_source_connection(None, "langfuse") == "ready"

    dotenv = (tmp_path / ".env.local").read_text()
    assert "ANTHROPIC_API_KEY='anthropic-test'" in dotenv
    assert "OPENAI_API_KEY" not in dotenv
    assert "KENSA_JUDGE_PROVIDER='anthropic'" in dotenv
    assert f"KENSA_JUDGE_MODEL='{DEFAULT_ANTHROPIC_JUDGE_MODEL}'" in dotenv
    assert checks[0]["public_key"] == "pk-test"


def test_init_langfuse_credentials_can_select_us_region(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    keys = iter(["\r", "\r", "j", "\r"])
    prompts = iter(["pk-test", "sk-test", "openai-test"])
    checks: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    monkeypatch.setattr(cli.click, "prompt", lambda *args, **kwargs: next(prompts))
    _stub_langfuse_auth_check(monkeypatch, checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert cli._configure_trace_source_connection(None, "langfuse") == "ready"

    dotenv = tmp_path / ".env.local"
    assert "LANGFUSE_BASE_URL='https://us.cloud.langfuse.com'" in dotenv.read_text()
    assert checks[0]["endpoint"] == "https://us.cloud.langfuse.com"


def test_init_langfuse_credentials_can_select_custom_base_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    keys = iter(["\r", "\r", "j", "j", "j", "j", "\r"])
    prompts = iter(["pk-test", "sk-test", "openai-test", "https://langfuse.internal.test"])
    checks: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    monkeypatch.setattr(cli.click, "prompt", lambda *args, **kwargs: next(prompts))
    _stub_langfuse_auth_check(monkeypatch, checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert cli._configure_trace_source_connection(None, "langfuse") == "ready"

    dotenv = tmp_path / ".env.local"
    assert "LANGFUSE_BASE_URL='https://langfuse.internal.test'" in dotenv.read_text()
    assert checks[0]["endpoint"] == "https://langfuse.internal.test"


def test_dotenv_values_do_not_expand_environment_references(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("SECRET", raising=False)
    monkeypatch.setenv("INJECTED", "changed")
    dotenv = tmp_path / ".env.local"
    dotenv.write_text("SECRET='old'\n")

    cli._write_dotenv_values(dotenv, {"SECRET": "abc${INJECTED}def"})
    monkeypatch.setenv(cli._DOTENV_ENV_VAR, str(dotenv))
    cli._load_startup_dotenv()

    assert dotenv.read_text() == "SECRET='abc${INJECTED}def'\n"
    assert os.environ["SECRET"] == "abc${INJECTED}def"


def test_provider_init_env_values_default_noninteractive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)

    assert cli._provider_init_env_values("langfuse", tmp_path / ".env.local") == {
        "LANGFUSE_BASE_URL": "https://cloud.langfuse.com"
    }


def test_provider_default_env_values_preserve_existing_base_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    dotenv = tmp_path / ".env.local"
    dotenv.write_text("LANGFUSE_BASE_URL='https://langfuse.internal.test'\n")

    assert cli._provider_default_env_values("langfuse", dotenv) == {}


def test_init_langfuse_preserves_existing_base_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    (tmp_path / "pyproject.toml").write_text('[tool.kensa]\ndotenv = ".env.local"\n')
    dotenv = tmp_path / ".env.local"
    dotenv.write_text("LANGFUSE_BASE_URL='https://self-hosted.langfuse.test'\n")
    keys = iter(["\r", "\r"])
    prompts = iter(["pk-test", "sk-test", "openai-test"])
    checks: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    monkeypatch.setattr(cli.click, "prompt", lambda *args, **kwargs: next(prompts))
    _stub_langfuse_auth_check(monkeypatch, checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert cli._configure_trace_source_connection(None, "langfuse") == "ready"

    assert "https://self-hosted.langfuse.test" in dotenv.read_text()
    assert "https://cloud.langfuse.com" not in dotenv.read_text()
    assert checks[0]["endpoint"] == "https://self-hosted.langfuse.test"


def test_discover_dotenv_files_scans_only_from_selected_root(tmp_path: Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    (tmp_path / ".env").write_text("OUTSIDE=true\n")
    (root / ".env.local").write_text("LOCAL=true\n")
    (root / ".env.development").write_text("DEV=true\n")
    (root / "README.md").write_text("not env\n")
    config = root / "config"
    config.mkdir()
    (config / "dev.env").write_text("CONFIG=true\n")
    ignored = root / "node_modules"
    ignored.mkdir()
    (ignored / ".env").write_text("IGNORED=true\n")
    kensa = root / ".kensa"
    kensa.mkdir()
    (kensa / "dev.env").write_text("IGNORED=true\n")

    assert cli._discover_dotenv_files(root) == (
        Path(".env.development"),
        Path(".env.local"),
        Path("config/dev.env"),
    )


def test_discover_dotenv_files_caps_results(tmp_path: Path) -> None:
    for index in range(cli._DOTENV_DISCOVERY_LIMIT + 1):
        (tmp_path / f"{index:02}.env").write_text("KEY=value\n")

    assert len(cli._discover_dotenv_files(tmp_path)) == cli._DOTENV_DISCOVERY_LIMIT


def test_select_existing_dotenv_path_accepts_manual_absolute_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    selected = tmp_path / "custom.env"
    keys = iter(["\r"])
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    monkeypatch.setattr(cli.click, "prompt", lambda *args, **kwargs: str(selected))

    assert cli._select_existing_dotenv_path() == selected


def test_init_langfuse_can_use_existing_env_file_without_editing_it(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    config = tmp_path / "config"
    config.mkdir()
    dotenv = config / "dev.env"
    original = "\n".join(
        [
            "LANGFUSE_PUBLIC_KEY=pk-existing",
            "LANGFUSE_SECRET_KEY=sk-existing",
            "OPENAI_API_KEY=openai-existing",
            "LANGFUSE_BASE_URL=https://us.cloud.langfuse.com",
            "",
        ]
    )
    dotenv.write_text(original)
    keys = iter(["j", "\r", "\r"])
    checks: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    _stub_langfuse_auth_check(monkeypatch, checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert cli._configure_trace_source_connection(None, "langfuse") == "ready"

    output = capsys.readouterr().out
    assert dotenv.read_text() == original
    assert not (tmp_path / ".env.local").exists()
    assert not (tmp_path / ".gitignore").exists()
    assert tomllib.loads((tmp_path / "pyproject.toml").read_text())["tool"]["kensa"] == {
        "dotenv": "config/dev.env"
    }
    assert checks[0]["public_key"] == "pk-existing"
    assert checks[0]["secret_key"] == "sk-existing"
    assert checks[0]["endpoint"] == "https://us.cloud.langfuse.com"
    assert "pk-existing" not in output
    assert "sk-existing" not in output
    assert "openai-existing" not in output


def test_init_langfuse_uses_configured_dotenv_without_prompting(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    (tmp_path / "pyproject.toml").write_text('[tool.kensa]\ndotenv = "config/dev.env"\n')
    config = tmp_path / "config"
    config.mkdir()
    dotenv = config / "dev.env"
    dotenv.write_text(
        "LANGFUSE_PUBLIC_KEY=pk-existing\n"
        "LANGFUSE_SECRET_KEY=sk-existing\n"
        "OPENAI_API_KEY=openai-existing\n"
    )
    checks: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cli.click,
        "getchar",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected prompt")),
    )
    monkeypatch.setattr(cli, "_git_tracks_path", lambda path: path == dotenv)
    _stub_langfuse_auth_check(monkeypatch, checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert cli._configure_trace_source_connection(None, "langfuse") == "ready"

    output = capsys.readouterr().out
    assert "using credentials from config/dev.env" in output
    assert "config/dev.env is tracked by git" in output
    assert checks[0]["public_key"] == "pk-existing"
    assert "pk-existing" not in output


def test_init_langfuse_uses_configured_dotenv_without_judge_key(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    (tmp_path / "pyproject.toml").write_text('[tool.kensa]\ndotenv = "config/dev.env"\n')
    config = tmp_path / "config"
    config.mkdir()
    dotenv = config / "dev.env"
    dotenv.write_text("LANGFUSE_PUBLIC_KEY=pk-existing\nLANGFUSE_SECRET_KEY=sk-existing\n")
    checks: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cli.click,
        "getchar",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected prompt")),
    )
    _stub_langfuse_auth_check(monkeypatch, checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert cli._configure_trace_source_connection(None, "langfuse") == "ready"

    output = capsys.readouterr().out
    connection = json.loads((tmp_path / ".kensa" / "connections" / "langfuse.json").read_text())
    assert "using credentials from config/dev.env" in output
    assert "LLM-as-judge key not found" in output
    assert "OPENAI_API_KEY or ANTHROPIC_API_KEY" in output
    assert checks[0]["public_key"] == "pk-existing"
    assert connection["auth"]["public_key_env"] == "LANGFUSE_PUBLIC_KEY"
    assert connection["auth"]["secret_key_env"] == "LANGFUSE_SECRET_KEY"
    assert "pk-existing" not in output
    assert "sk-existing" not in output


def test_init_langfuse_existing_env_file_connects_without_judge_key(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    dotenv = tmp_path / "dev.env"
    dotenv.write_text("LANGFUSE_PUBLIC_KEY=pk-existing\nLANGFUSE_SECRET_KEY=sk-existing\n")
    keys = iter(["j", "\r", "\r"])
    checks: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    _stub_langfuse_auth_check(monkeypatch, checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert cli._configure_trace_source_connection(None, "langfuse") == "ready"

    captured = capsys.readouterr()
    connection = json.loads((tmp_path / ".kensa" / "connections" / "langfuse.json").read_text())
    assert "LLM-as-judge key not found" in captured.out
    assert "OPENAI_API_KEY or ANTHROPIC_API_KEY" in captured.out
    assert "saved metadata: .kensa/connections/langfuse.json" in captured.out
    assert checks[0]["public_key"] == "pk-existing"
    assert connection["provider"] == "langfuse"
    assert "pk-existing" not in captured.out
    assert "sk-existing" not in captured.out
    assert "pk-existing" not in captured.err
    assert "sk-existing" not in captured.err


def test_init_langfuse_existing_env_file_warns_for_unsupported_judge_provider(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    dotenv = tmp_path / "dev.env"
    dotenv.write_text(
        "LANGFUSE_PUBLIC_KEY=pk-existing\n"
        "LANGFUSE_SECRET_KEY=sk-existing\n"
        "KENSA_JUDGE_PROVIDER=gemini\n"
        "OPENAI_API_KEY=openai-existing\n"
    )
    keys = iter(["j", "\r", "\r"])
    checks: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    _stub_langfuse_auth_check(monkeypatch, checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert cli._configure_trace_source_connection(None, "langfuse") == "ready"

    captured = capsys.readouterr()
    normalized = " ".join(captured.out.split())
    assert "Unsupported KENSA_JUDGE_PROVIDER: gemini" in captured.out
    assert "built-in judge supports openai and anthropic" in normalized
    assert "set_judge_provider()" in normalized
    assert "LLM-as-judge key not found" not in captured.out
    assert "OPENAI_API_KEY or ANTHROPIC_API_KEY" not in captured.out
    assert "saved metadata: .kensa/connections/langfuse.json" in captured.out
    assert checks[0]["public_key"] == "pk-existing"
    assert "Unsupported KENSA_JUDGE_PROVIDER" not in captured.err


def test_init_langfuse_existing_env_file_warns_for_unsupported_judge_provider_without_key(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    dotenv = tmp_path / "dev.env"
    dotenv.write_text(
        "LANGFUSE_PUBLIC_KEY=pk-existing\n"
        "LANGFUSE_SECRET_KEY=sk-existing\n"
        "KENSA_JUDGE_PROVIDER=gemini\n"
    )
    keys = iter(["j", "\r", "\r"])
    checks: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    _stub_langfuse_auth_check(monkeypatch, checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert cli._configure_trace_source_connection(None, "langfuse") == "ready"

    captured = capsys.readouterr()
    normalized = " ".join(captured.out.split())
    assert "Unsupported KENSA_JUDGE_PROVIDER: gemini" in captured.out
    assert "built-in judge supports openai and anthropic" in normalized
    assert "set_judge_provider()" in normalized
    assert "LLM-as-judge key not found" in captured.out
    assert "OPENAI_API_KEY or ANTHROPIC_API_KEY" in captured.out
    assert "saved metadata: .kensa/connections/langfuse.json" in captured.out
    assert checks[0]["public_key"] == "pk-existing"


def test_missing_init_judge_envs_checks_dotenv_without_loaded_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _clear_init_credential_env(monkeypatch)
    dotenv = tmp_path / "dev.env"
    dotenv.write_text("ANTHROPIC_API_KEY=anthropic-existing\n")

    assert cli._missing_init_judge_envs(dotenv) == ()
    monkeypatch.setenv("KENSA_JUDGE_PROVIDER", "openai")
    assert cli._missing_init_judge_envs(dotenv) == ("OPENAI_API_KEY",)
    assert cli._format_missing_init_judge_envs(("OPENAI_API_KEY",)) == "OPENAI_API_KEY"


def test_init_langfuse_existing_env_file_requires_langfuse_secret_key(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    dotenv = tmp_path / "dev.env"
    dotenv.write_text("LANGFUSE_PUBLIC_KEY=pk-existing\n")
    keys = iter(["j", "\r", "\r"])
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    monkeypatch.setattr(
        cli,
        "_connect_provider",
        lambda args: (_ for _ in ()).throw(AssertionError("unexpected connection check")),
    )

    assert cli._configure_trace_source_connection(None, "langfuse") == "failed"

    captured = capsys.readouterr()
    assert "Missing required credentials in dev.env:" in captured.err
    assert "LANGFUSE_SECRET_KEY" in captured.err
    assert "OPENAI_API_KEY" not in captured.err
    assert "pk-existing" not in captured.out
    assert "pk-existing" not in captured.err


def test_init_langfuse_existing_env_file_warns_for_unrecognized_judge_model(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    dotenv = tmp_path / "dev.env"
    dotenv.write_text(
        "LANGFUSE_PUBLIC_KEY=pk-existing\n"
        "LANGFUSE_SECRET_KEY=sk-existing\n"
        "KENSA_JUDGE_MODEL=custom-model\n"
    )
    keys = iter(["j", "\r", "\r"])
    checks: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    _stub_langfuse_auth_check(monkeypatch, checks)
    _reject_langfuse_trace_read(monkeypatch)

    assert cli._configure_trace_source_connection(None, "langfuse") == "ready"

    captured = capsys.readouterr()
    normalized = " ".join(captured.out.split())
    assert "Judge model configured (custom-model)" in captured.out
    assert "could not infer a built-in judge provider" in normalized
    assert "KENSA_JUDGE_PROVIDER" in normalized
    assert "set_judge_provider()" in normalized
    assert "LLM-as-judge key not found" in captured.out
    assert "OPENAI_API_KEY or ANTHROPIC_API_KEY" in captured.out
    assert checks[0]["public_key"] == "pk-existing"
    assert "pk-existing" not in captured.out
    assert "sk-existing" not in captured.out


def test_init_langfuse_explicit_judge_provider_wins_over_unrecognized_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _clear_init_credential_env(monkeypatch)
    monkeypatch.setenv("KENSA_JUDGE_PROVIDER", "openai")
    monkeypatch.setenv("KENSA_JUDGE_MODEL", "o3-mini")

    assert cli._missing_init_judge_envs(tmp_path / ".env.local") == ("OPENAI_API_KEY",)


def test_init_langfuse_invalid_judge_provider_wins_over_unrecognized_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _clear_init_credential_env(monkeypatch)
    monkeypatch.setenv("KENSA_JUDGE_PROVIDER", "gemini")
    monkeypatch.setenv("KENSA_JUDGE_MODEL", "custom-model")

    with pytest.raises(ValueError, match="Unsupported KENSA_JUDGE_PROVIDER: gemini"):
        cli._missing_init_judge_envs(tmp_path / ".env.local")


def test_init_langfuse_existing_env_file_skips_judge_notice_for_local_result(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    dotenv = tmp_path / "dev.env"
    dotenv.write_text(
        "LANGFUSE_PUBLIC_KEY=pk-existing\n"
        "LANGFUSE_SECRET_KEY=sk-existing\n"
        "KENSA_JUDGE_RESULT=pass\n"
    )
    monkeypatch.setenv("KENSA_JUDGE_RESULT", "pass")
    keys = iter(["j", "\r", "\r"])
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    _stub_langfuse_auth_check(monkeypatch)
    _reject_langfuse_trace_read(monkeypatch)

    assert cli._configure_trace_source_connection(None, "langfuse") == "ready"

    combined = "\n".join(capsys.readouterr())
    assert "LLM-as-judge key not found" not in combined
    assert "OPENAI_API_KEY or ANTHROPIC_API_KEY" not in combined
    assert cli._missing_any_init_judge_envs(dotenv) == ()


def test_init_provider_credentials_can_be_configured_later(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    keys = iter(["j", "j", "\r"])
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))

    assert cli._configure_trace_source_connection(None, "langfuse") == "deferred"

    output = capsys.readouterr().out
    assert "Credentials not configured" in output
    assert "LANGFUSE_PUBLIC_KEY" in output
    assert "LANGFUSE_SECRET_KEY" in output
    assert "LLM provider key" not in output
    assert not (tmp_path / ".env.local").exists()


def test_init_provider_connection_failure_marks_setup_incomplete(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeSteps:
        def __init__(self) -> None:
            self.steps: list[str] = []
            self.items: list[tuple[str, bool]] = []

        def step(self, text: str) -> None:
            self.steps.append(text)

        def item(self, text: str, *, ok: bool = True) -> None:
            self.items.append((text, ok))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    keys = iter(["\r", "\r", "\r"])
    prompts = iter(["pk-test", "sk-test", "openai-test"])
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    monkeypatch.setattr(cli.click, "prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.setattr(
        cli,
        "_connect_provider",
        lambda args: (_ for _ in ()).throw(ValueError("bad credentials")),
    )
    fake_steps = FakeSteps()
    steps = cast(cli._Steps, fake_steps)

    assert cli._configure_trace_source_connection(steps, "langfuse") == "failed"

    assert fake_steps.steps == ["Configure credentials"]
    assert fake_steps.items == [("Langfuse setup failed: bad credentials", False)]


def test_init_langfuse_connection_error_does_not_print_raw_urlopen_details(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    _clear_init_credential_env(monkeypatch)
    keys = iter(["\r", "\r", "\r"])
    prompts = iter(["pk-test", "sk-test", "openai-test"])
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))
    monkeypatch.setattr(cli.click, "prompt", lambda *args, **kwargs: next(prompts))

    def fail_connect(args: Any) -> tuple[dict[str, Any], Path, Any]:
        del args
        raise ValueError(
            "Langfuse could not be reached while fetching projects.\n"
            "Endpoint: https://cloud.langfuse.com\n"
            "Kensa could not resolve the Langfuse host.\n"
            "Then run: kensa connect langfuse"
        )

    monkeypatch.setattr(cli, "_connect_provider", fail_connect)

    assert cli._configure_trace_source_connection(None, "langfuse") == "failed"

    captured = capsys.readouterr()
    combined = f"{captured.out}\n{captured.err}"
    assert "Langfuse credentials were saved, but Kensa could not verify the connection." in combined
    assert "Kensa could not resolve the Langfuse host." in combined
    assert "Run kensa connect langfuse after fixing it." in combined
    assert "urlopen error" not in combined
    assert "nodename nor servname" not in combined


def test_init_langfuse_friendly_connection_error_without_saved_dotenv(capsys) -> None:
    cli._init_connection_failure(
        None,
        label="Langfuse",
        provider="langfuse",
        exc=ValueError("Langfuse could not be reached while fetching projects."),
        dotenv_path=None,
        credential_source="existing",
    )

    combined = "\n".join(capsys.readouterr())
    assert "Langfuse connection verification failed." in combined
    assert "Langfuse setup failed" not in combined


def test_init_provider_helper_errors_are_explicit() -> None:
    with pytest.raises(ValueError, match="unsupported connection provider"):
        cli._provider_env_names("bad")
    with pytest.raises(ValueError, match="unsupported connection provider"):
        cli._provider_default_env_values("bad", Path(".env.local"))
    with pytest.raises(ValueError, match="unsupported connection provider"):
        cli._provider_init_env_values("bad", Path(".env.local"))
    with pytest.raises(ValueError, match="unsupported connection provider"):
        cli._init_connect_args("bad")
    with pytest.raises(ValueError, match="cannot contain newlines"):
        cli._validated_dotenv_value("SECRET", "bad\nvalue")
    with pytest.raises(ValueError, match="cannot be empty"):
        cli._validated_dotenv_value("SECRET", "")
    with pytest.raises(ValueError, match="cannot contain single quotes"):
        cli._validated_dotenv_value("SECRET", "bad'value")


def test_init_credential_helper_branches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _clear_init_credential_env(monkeypatch)
    dotenv = tmp_path / ".env.local"
    assert cli._init_credential_source_label("bad", "Langfuse", dotenv) == "bad"
    assert cli._judge_provider_label("other") == "other"
    assert cli._judge_provider_for_model("gpt-5.5") == "openai"
    assert cli._judge_provider_for_model("claude-sonnet-4-6") == "anthropic"
    assert cli._judge_provider_for_model("custom") is None

    monkeypatch.setenv("OPENAI_API_KEY", "openai-test")
    assert cli._judge_provider_from_environment() == "openai"
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setenv("KENSA_JUDGE_PROVIDER", "anthropic")
    assert cli._judge_provider_from_environment() == "anthropic"
    monkeypatch.setenv("KENSA_JUDGE_PROVIDER", "bogus")
    with pytest.raises(ValueError, match="built-in judge supports openai"):
        cli._judge_provider_from_environment()
    monkeypatch.delenv("KENSA_JUDGE_PROVIDER")
    monkeypatch.setenv("KENSA_JUDGE_MODEL", "claude-sonnet-4-6")
    assert cli._judge_provider_from_environment() == "anthropic"
    monkeypatch.delenv("KENSA_JUDGE_MODEL")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test")
    assert cli._judge_provider_from_environment() == "anthropic"


def test_steps_notice_renders_warning(monkeypatch) -> None:
    console = Console(record=True, highlight=False)
    monkeypatch.setattr(cli_output, "CONSOLE", console)

    steps = cli._Steps()
    steps.notice("check lifecycle")

    assert "! check lifecycle" in console.export_text()


def test_agent_instruction_path_for_choice_handles_other() -> None:
    assert cli._agent_instruction_path_for_choice("other") == Path(
        ".agents/skills/kensa-evals/SKILL.md"
    )


def test_init_notice_forwards_to_interactive_steps() -> None:
    class FakeSteps:
        def __init__(self) -> None:
            self.notices: list[str] = []

        def notice(self, text: str) -> None:
            self.notices.append(text)

    fake_steps = FakeSteps()
    steps = cast(cli._Steps, fake_steps)

    cli._init_notice(steps, "carry on")

    assert fake_steps.notices == ["carry on"]


def test_git_tracks_path_returns_false_when_git_cannot_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("git missing")),
    )

    assert cli._git_tracks_path(tmp_path / ".env") is False


def test_record_pyproject_dotenv_updates_existing_pyproject(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"')

    cli._record_pyproject_dotenv(tmp_path / ".env.local")

    assert tomllib.loads((tmp_path / "pyproject.toml").read_text())["tool"]["kensa"] == {
        "dotenv": ".env.local"
    }

    subproject = tmp_path / "subproject"
    subproject.mkdir()
    monkeypatch.chdir(subproject)
    (subproject / "pyproject.toml").write_text("[tool.kensa]\nother = true\n")

    cli._record_pyproject_dotenv(subproject / ".env.local")

    assert tomllib.loads((subproject / "pyproject.toml").read_text())["tool"]["kensa"] == {
        "dotenv": ".env.local",
        "other": True,
    }

    (subproject / "pyproject.toml").write_text("[tool.kensa]\nother = true\n[project]\n")
    cli._record_pyproject_dotenv(subproject / ".env.local")

    assert tomllib.loads((subproject / "pyproject.toml").read_text())["tool"]["kensa"] == {
        "dotenv": ".env.local",
        "other": True,
    }

    (subproject / "pyproject.toml").write_text('[tool.kensa]\ndotenv = ""\nother = true\n')
    cli._record_pyproject_dotenv(subproject / ".env.local")

    assert tomllib.loads((subproject / "pyproject.toml").read_text())["tool"]["kensa"] == {
        "dotenv": ".env.local",
        "other": True,
    }


def test_dotenv_has_key_ignores_non_key_lines(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env.local"
    dotenv.write_text("\n# comment\nnot-a-key\nOTHER='yes'\n")

    assert cli._dotenv_has_key(dotenv, "LANGFUSE_BASE_URL") is False


def test_init_dotenv_paths_are_pyproject_relative_from_subdirectories(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    subdir = tmp_path / "packages" / "agent"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    dotenv = cli._init_dotenv_path()
    cli._ensure_gitignored(dotenv)
    cli._record_pyproject_dotenv(dotenv)

    assert dotenv == tmp_path / ".env.local"
    assert (tmp_path / ".gitignore").read_text() == ".env.local\n"
    assert not (subdir / ".gitignore").exists()
    assert tomllib.loads((tmp_path / "pyproject.toml").read_text())["tool"]["kensa"] == {
        "dotenv": ".env.local"
    }


def test_init_interactive_interrupt_uses_step_exit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(
        cli,
        "_cmd_init_inner",
        lambda steps, evidence_source=None, agent_choice=None: (_ for _ in ()).throw(
            KeyboardInterrupt
        ),
    )

    assert main(["init"]) == 130

    captured = capsys.readouterr()
    assert "interrupted." in captured.out
    assert "Stopped" in captured.out
    assert captured.err == ""


def test_init_auto_detection_prefers_repo_marker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# Claude\n")
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda command: f"/bin/{command}" if command in {"codex", "claude"} else None,
    )

    assert main(["init"]) == 0

    assert (tmp_path / ".claude" / "skills" / "kensa-evals" / "SKILL.md").exists()
    assert not (tmp_path / ".agents" / "skills" / "kensa-evals" / "SKILL.md").exists()
    assert not (tmp_path / ".cursor" / "skills" / "kensa-evals" / "SKILL.md").exists()


def test_init_workflow_supports_uv_and_pip_install_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()

    assert main(["init"]) == 0

    workflow = (tmp_path / ".github" / "workflows" / "kensa.yml").read_text()
    assert "hashFiles('uv.lock')" in workflow
    assert "if: ${{ hashFiles('uv.lock') == '' }}" in workflow
    assert "uv sync" in workflow
    assert "python -m pip install -r requirements.txt" in workflow
    assert "grep -Eq '^[[:space:]]*\\[(project|build-system)\\]'" in workflow
    assert "python -m pip install -e ." in workflow
    assert 'if ! python -c "import kensa" >/dev/null 2>&1; then' in workflow
    assert "python -m pip install kensa" in workflow
    assert 'uv run python -c "import kensa"' in workflow
    assert "uv run kensa eval" in workflow
    assert "uv run --with kensa kensa eval" in workflow
    assert "            kensa eval" in workflow
    assert "--require-durable" not in workflow


def test_codex_smoke_only_failure_mode_requires_durable_eval(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()

    assert main(["init"]) == 0
    capsys.readouterr()
    _write_ready_harness(tmp_path / "tests" / "evals")

    assert main(["doctor"]) == 0
    capsys.readouterr()
    assert main(["eval", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["pytest"]["returncode"] == 0
    assert payload["data"]["evals_readiness"]["ready"] is False
    assert "--require-durable" not in (tmp_path / ".github" / "workflows" / "kensa.yml").read_text()


def test_print_init_added_files_empty(capsys) -> None:
    cli._print_init_added_files([])

    assert "No new files added." in capsys.readouterr().out


def test_setup_pr_step_uses_gh_when_available(monkeypatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda command: "/bin/gh" if command == "gh" else None)

    assert cli._setup_pr_step() == "Open the setup PR with gh pr create --fill."
    steps = cli._setup_handoff_next_steps()
    assert steps[3] == "Let kensa-evals read .kensa/settings.json for the selected trace source."
    assert steps[-1] == "Open the setup PR with gh pr create --fill."


@pytest.mark.parametrize(
    ("source_index", "expected_source"),
    [
        (0, "langfuse"),
        (1, "trace_export"),
        (2, "local"),
    ],
)
def test_init_interactive_trace_source_choice_prints_two_step_handoff(
    source_index: int,
    expected_source: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda command: "/bin/codex" if command == "codex" else None,
    )
    monkeypatch.setattr(cli, "_configure_trace_source_connection", lambda steps, source: "ready")
    monkeypatch.setattr(cli, "_configure_redaction_readiness", lambda steps, source: "ready")
    keys = iter(["\r", *(["j"] * source_index), "\r"])
    monkeypatch.setattr(cli.click, "getchar", lambda **kwargs: next(keys))

    assert main(["init"]) == 0

    output = capsys.readouterr().out
    assert "Where should Kensa get traces from?" in output
    assert "Note: traces will be auto redacted during import." in output
    assert "Existing trace export" in output
    assert "Langfuse" in output
    assert "No traces? Capture local run" in output
    assert "Next steps:" in output
    assert "1. Open Claude Code." in output
    assert (
        '2. Paste this: "Use /kensa-evals to continue the Kensa lifecycle for this repo."' in output
    )
    assert "Selected trace source:" not in output
    assert "Copyable setup prompt" not in output
    assert "Setup is complete when" not in output
    assert _read_settings(tmp_path)["init"]["evidence_source"] == expected_source


def test_setup_handoff_prompt_is_generic_and_settings_driven() -> None:
    prompt = cli._setup_handoff_prompt()

    assert "\n" not in prompt
    assert "Use the generated kensa-evals skill" in prompt
    assert "setup -> evidence -> inspect -> approval -> generate -> verify" in prompt
    assert "Start with state detection" in prompt
    assert "Read .kensa/settings.json for the selected trace source" in prompt
    assert "Detect credentials by name only" in prompt
    assert "never read or print API keys or .env files" in prompt
    assert "kensa-setup" not in prompt
    assert "kensa-inspect" not in prompt
    assert "kensa-generate" not in prompt
    assert "kensa connect langfuse" not in prompt
    assert "kensa import --from" not in prompt


def test_init_noninteractive_prints_generic_trace_source_handoff(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("shutil.which", lambda command: None)

    code = main(["init"])

    assert code == 0
    output = capsys.readouterr().out
    assert "░███████" not in output
    assert "Where should Kensa get traces from?" not in output
    assert "Copyable setup prompt" in output
    assert "setup -> evidence -> inspect -> approval -> generate -> verify" in output
    assert ".kensa/settings.json" in output
    assert "kensa connect langfuse" not in output
    assert "kensa import --from langfuse --limit 50" not in output
    assert "kensa.instrument()" not in output
    assert "KENSA_TRACE_DIR" not in output
    assert "kensa import --from <provider> --source <file>" not in output
    assert "kensa-evals" in output
    assert "kensa-setup" not in output
    assert "kensa-inspect" not in output
    assert "kensa-generate" not in output
    assert "kensa eval" not in output
    assert "Use the generated kensa-evals skill" in cli.SETUP_HANDOFF_PROMPT
    assert "source-specific instruction" not in cli.SETUP_HANDOFF_PROMPT
    assert "evidence_source" not in _read_settings(tmp_path)["init"]
    assert (
        "kensa-evals`: setup -> evidence -> inspect -> approval -> generate -> verify"
        in (REPO_ROOT / "README.md").read_text()
    )
    assert "Next steps" in output
    assert "1. Open your coding agent." in output
    assert (
        '2. Paste this: "Use kensa-evals to continue the Kensa lifecycle for this repo."' in output
    )
    assert (
        "3. Follow the lifecycle: setup -> evidence -> inspect -> approval -> generate -> verify."
        in output
    )
    assert "4. Let kensa-evals read .kensa/settings.json" in output
    assert "5. Commit the harness changes; run gh pr create --fill" in output
    assert "when gh auth is" in output
    assert "gh pr create" in output


def test_init_does_not_require_agent_or_doctor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)
    monkeypatch.setattr(
        cli,
        "_run_doctor_check",
        lambda: (_ for _ in ()).throw(AssertionError("init should not run doctor")),
    )

    code = main(["init"])

    assert code == 0
    output = capsys.readouterr().out
    assert "wiring kensa_run with" not in output
    assert "Kensa setup files created." in output
    assert "No agent skill files installed (no supported agent detected)" in output
    assert ".agents/skills" in output
    assert ".claude/skills" in output
    assert ".cursor/skills" in output
    assert (tmp_path / "tests" / "evals" / "conftest.py").exists()
    assert (tmp_path / "tests" / "evals" / "test_kensa_smoke.py").exists()
    for skill_name in PACKAGED_SKILLS:
        assert not (tmp_path / ".agents" / "skills" / skill_name / "SKILL.md").exists()
        assert not (tmp_path / ".claude" / "skills" / skill_name / "SKILL.md").exists()
        assert not (tmp_path / ".cursor" / "skills" / skill_name / "SKILL.md").exists()
    assert not (tmp_path / ".kensa" / "init-failure.md").exists()


def test_agent_skill_steps_copy_uses_agent_markers() -> None:
    assert cli._agent_skill_steps("codex") == (
        "Open Codex.",
        'Paste this: "Use $kensa-evals to continue the Kensa lifecycle for this repo."',
    )
    assert cli._agent_skill_steps("cursor") == (
        "Open Cursor.",
        'Paste this: "Use /kensa-evals to continue the Kensa lifecycle for this repo."',
    )
    assert cli._agent_skill_steps("claude") == (
        "Open Claude Code.",
        'Paste this: "Use /kensa-evals to continue the Kensa lifecycle for this repo."',
    )
    assert cli._agent_skill_steps("all") == (
        "Open your coding agent.",
        'Paste the matching command: Codex "$kensa-evals"; Claude Code or Cursor "/kensa-evals".',
    )
    assert cli._agent_skill_steps("future") == (
        "Open your coding agent.",
        'Paste this: "Use kensa-evals to continue the Kensa lifecycle for this repo."',
    )
    assert cli._agent_instruction_key_for_path(Path("unknown")) is None


@pytest.mark.parametrize("flag", ["--scaffold-only", "--max-agent-attempts", "--quiet"])
def test_removed_init_flags_are_rejected(flag: str, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["init", flag]) == 2
    assert not (tmp_path / "tests" / "evals" / "conftest.py").exists()


def test_local_trace_commands_support_jsonl_source(tmp_path: Path, capsys) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps(_trace_view_row("tr_1", span_count=1)),
                json.dumps(_trace_view_row("tr_2", status="error")),
            ]
        )
        + "\n"
    )
    _write_safe_import_manifest(source)

    assert (
        cli_traces.cmd_traces(
            SimpleNamespace(traces_command="list", source=str(source), json=False)
        )
        == 0
    )
    assert "tr_1" in capsys.readouterr().out
    assert (
        cli_traces.cmd_traces(
            SimpleNamespace(
                traces_command="get",
                source=str(source),
                trace_id="tr_2",
                json=False,
            )
        )
        == 0
    )
    assert '"tr_2"' in capsys.readouterr().out
    assert (
        cli_traces.cmd_traces(
            SimpleNamespace(traces_command="sample", source=str(source), json=False)
        )
        == 0
    )
    assert '"tr_1"' in capsys.readouterr().out


def test_local_trace_commands_support_json_output(tmp_path: Path, capsys) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps(_trace_view_row("tr_1", span_count=1)),
                json.dumps(_trace_view_row("tr_2", status="error")),
            ]
        )
        + "\n"
    )
    _write_safe_import_manifest(source)

    assert (
        cli_traces.cmd_traces(SimpleNamespace(traces_command="list", source=str(source), json=True))
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "traces list"
    assert [trace["id"] for trace in payload["data"]["traces"]] == ["tr_1", "tr_2"]
    assert set(payload["data"]["traces"][0]) == {
        "id",
        "name",
        "status",
        "started_at_unix_nano",
        "duration_ms",
        "span_count",
        "source",
    }
    assert payload["data"]["traces"][0]["source"] == {
        "provider": "jsonl",
        "trace_url": None,
    }

    assert (
        cli_traces.cmd_traces(
            SimpleNamespace(
                traces_command="get",
                source=str(source),
                trace_id="tr_2",
                json=True,
            )
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["trace"]["id"] == "tr_2"

    assert (
        cli_traces.cmd_traces(
            SimpleNamespace(traces_command="sample", source=str(source), json=True)
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["trace"]["id"] == "tr_1"

    assert (
        cli_traces.cmd_traces(
            SimpleNamespace(
                traces_command="get",
                source=str(source),
                trace_id="missing",
                json=True,
            )
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["errors"] == ["trace not found: missing"]


def test_trace_commands_use_latest_pointer_and_reject_old_artifacts(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    artifact = tmp_path / ".kensa" / "traces" / "imports" / "run" / "traces.jsonl"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps(_trace_view_row("tr_latest")) + "\n")
    _write_safe_import_manifest(artifact)
    latest = tmp_path / ".kensa" / "traces" / "imports" / "latest.json"
    latest.write_text(
        json.dumps(
            {
                "schema_version": "kensa.trace_import_latest.v1",
                "artifact_path": str(artifact.relative_to(tmp_path)),
                "records_written": 1,
                "span_count": 0,
            }
        )
    )

    assert (
        cli_traces.cmd_traces(SimpleNamespace(traces_command="sample", source=None, json=True)) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["trace"]["id"] == "tr_latest"

    old_artifact = tmp_path / "old-traces.jsonl"
    old_artifact.write_text('{"id":"tr_old","spans":[]}\n')
    old_artifact.with_suffix(".manifest.json").write_text(
        json.dumps({"redaction": {"version": "kensa.redactor.v1", "mode": "strict"}})
    )
    assert (
        cli_traces.cmd_traces(
            SimpleNamespace(traces_command="list", source=str(old_artifact), json=False)
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "Re-import traces with kensa import" in " ".join(captured.err.split())

    malformed_latest = tmp_path / ".kensa" / "traces" / "imports" / "latest.json"
    malformed_latest.write_text("{}")
    assert (
        cli_traces.cmd_traces(SimpleNamespace(traces_command="list", source=None, json=False)) == 1
    )
    captured = capsys.readouterr()
    assert "Latest trace import pointer is malformed" in captured.err

    assert (
        cli._cmd_import(
            SimpleNamespace(
                provider="local-jsonl",
                source=str(artifact),
                endpoint=None,
                project=None,
                since=None,
                out=None,
                limit=None,
                max_payload_bytes=1_000_000,
                json=False,
            )
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "Unsupported import provider" in captured.err
    assert "local-jsonl" in captured.err


def test_console_entrypoint_smoke() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "kensa.cli", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "░███████" in completed.stdout
    assert "eval" in completed.stdout


def test_doctor_reports_redaction_readiness_and_unsafe_artifacts(
    tmp_path: Path,
    monkeypatch,
    capsys,
    fake_redaction,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "_run_persistent_smoke",
        lambda: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )
    fake_redaction.make_ready(tmp_path, monkeypatch)
    settings_payload = json.loads((tmp_path / ".kensa" / "settings.json").read_text())
    settings_payload["init"] = {"evidence_source": "langfuse"}
    (tmp_path / ".kensa" / "settings.json").write_text(json.dumps(settings_payload))
    unsafe_artifact = tmp_path / ".kensa" / "traces" / "imports" / "old.jsonl"
    unsafe_artifact.parent.mkdir(parents=True)
    unsafe_artifact.write_text("{}\n")
    unsafe_artifact.with_suffix(".manifest.json").write_text(
        json.dumps({"redaction": {"version": "kensa.redactor.v1"}})
    )
    safe_artifact = tmp_path / ".kensa" / "traces" / "imports" / "new.jsonl"
    safe_artifact.write_text("{}\n")
    _write_safe_import_manifest(safe_artifact)
    tampered_artifact = tmp_path / ".kensa" / "traces" / "imports" / "tampered.jsonl"
    tampered_artifact.write_text("{}\n")
    _write_safe_import_manifest(tampered_artifact)
    tampered_artifact.write_text('{"input":"alice@example.com"}\n')

    assert main(["doctor", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    redaction = payload["data"]["redaction"]
    assert redaction["ready"] is True
    assert redaction["readiness"] == {
        "model": "en_core_web_sm",
        "model_version": "3.8.0",
        "checksum_verified": True,
    }
    assert redaction["dependencies"] == {
        "spacy": True,
        "presidio_analyzer": True,
        "detect_secrets": True,
        "phonenumbers": True,
    }
    assert redaction["unsafe_artifacts"] == [
        ".kensa/traces/imports/old.jsonl",
        ".kensa/traces/imports/tampered.jsonl",
    ]
    assert any("re-import required" in warning for warning in payload["warnings"])
    assert "Re-import blocked trace artifacts with kensa import." in payload["next_steps"]

    assert main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "Redaction dependencies: present" in output
    assert "Redaction readiness: ready, model en_core_web_sm-3.8.0" in output
    assert "Unsafe trace artifact (re-import required)" in output


def test_doctor_reports_missing_redaction_readiness_first(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "_run_persistent_smoke",
        lambda: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )
    settings_path = tmp_path / ".kensa" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"schema_version": "kensa.settings.v1", "init": {"evidence_source": "local"}})
    )
    monkeypatch.setattr(
        cli.redact,
        "missing_redaction_dependencies",
        lambda: cli.redact.REDACTION_EXTRA_MODULES,
    )

    assert main(["doctor", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    redaction = payload["data"]["redaction"]
    assert redaction["ready"] is False
    assert "dependencies are missing" in redaction["readiness_error"]
    assert any("dependencies missing" in warning for warning in payload["warnings"])
    assert any("not ready" in error for error in payload["errors"])
    assert "Run kensa init to bootstrap mandatory trace redaction." in payload["next_steps"]

    assert main(["doctor"]) == 1
    output = capsys.readouterr().out
    # Dependency presence is reported before readiness.
    assert output.index("Redaction dependencies missing") < output.index(
        "Redaction readiness: missing"
    )


def test_doctor_handles_unreadable_redaction_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    def fail_readiness(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise cli.redact.RedactionNotReadyError("invalid readiness")

    monkeypatch.setattr(cli.redact, "assert_redaction_ready", fail_readiness)
    monkeypatch.setattr(cli.redact, "read_redaction_readiness", fail_readiness)

    report = cli._doctor_redaction_report()

    assert report["ready"] is False
    assert report["readiness"] is None
    assert report["readiness_error"] == "invalid readiness"


def test_redaction_install_command_and_argv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli._redaction_install_command() == "pip install 'kensa[redaction]'"
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "uv.lock").write_text("")
    assert cli._redaction_install_command() == "uv add --dev 'kensa[redaction]'"
    assert cli._redaction_install_argv("uv add --dev 'kensa[redaction]'") == [
        "uv",
        "add",
        "--dev",
        "kensa[redaction]",
    ]
    assert cli._redaction_install_argv("pip install 'kensa[redaction]'") == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "kensa[redaction]",
    ]


def test_ensure_redaction_dependencies_install_flows(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.redact, "missing_redaction_dependencies", lambda: ("spacy",))
    install_calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        install_calls.append((argv, kwargs))
        monkeypatch.setattr(cli.redact, "missing_redaction_dependencies", lambda: ())
        return SimpleNamespace(returncode=0, stdout="installer details")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli._ensure_redaction_dependencies(None) is True
    assert install_calls == [
        (
            [sys.executable, "-m", "pip", "install", "kensa[redaction]"],
            {
                "check": False,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
            },
        )
    ]
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(cli.redact, "missing_redaction_dependencies", lambda: ("spacy",))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout="resolver failed\n"),
    )
    assert cli._ensure_redaction_dependencies(None) is False
    error = capsys.readouterr().err
    assert "install failed; run pip install 'kensa[redaction]'" in error
    assert "resolver failed" in error


def test_ensure_redaction_dependencies_handles_missing_installer(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "uv.lock").write_text("")
    monkeypatch.setattr(cli.redact, "missing_redaction_dependencies", lambda: ("spacy",))

    def missing_installer(argv: list[str], **kwargs: Any) -> None:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(cli.subprocess, "run", missing_installer)

    assert cli._ensure_redaction_dependencies(None) is False
    error = capsys.readouterr().err
    assert "install failed; run uv add --dev 'kensa[redaction]'" in error
    assert "No such file or directory: 'uv'" in error


def test_configure_redaction_readiness_statuses(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli._configure_redaction_readiness(None, None) == "skipped"

    steps = cli._Steps()
    monkeypatch.setattr(cli, "_ensure_redaction_dependencies", lambda steps: False)
    assert cli._configure_redaction_readiness(steps, "langfuse") == "failed"
    assert cli._configure_redaction_readiness(steps, "trace_export") == "failed"
    assert cli._configure_redaction_readiness(steps, "local") == "failed"
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(cli, "_ensure_redaction_dependencies", lambda steps: True)

    def failing_bootstrap(root=None):
        raise cli.redact.RedactionBootstrapError("no model")

    monkeypatch.setattr(cli.redact, "ensure_redaction_ready", failing_bootstrap)
    assert cli._configure_redaction_readiness(steps, "langfuse") == "failed"
    assert cli._configure_redaction_readiness(steps, "trace_export") == "failed"
    assert cli._configure_redaction_readiness(steps, "local") == "failed"
    output = capsys.readouterr().out
    assert "redaction model bootstrap failed" in output
    assert "stay blocked" not in output

    readiness = cli.redact.RedactionReadiness(
        model="en_core_web_sm",
        model_version="3.8.0",
        checksum_verified=True,
    )
    monkeypatch.setattr(cli.redact, "ensure_redaction_ready", lambda root=None: readiness)
    assert cli._configure_redaction_readiness(steps, "trace_export") == "ready"
    output = capsys.readouterr().out
    assert "Configure sensitive data protection" not in output
    assert "traces will be auto redacted during import" not in output
    assert "redaction dependencies present" not in output
    assert "redaction model ready" not in output
    assert "readiness recorded" not in output


def test_redaction_init_failure_statuses() -> None:
    assert cli._redaction_init_failed("failed")
    assert not cli._redaction_init_failed("deferred")
    assert not cli._redaction_init_failed("ready")
    assert not cli._redaction_init_failed("skipped")


def test_trace_commands_use_artifact_manifest_without_settings(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "traces.jsonl"
    source.write_text(json.dumps(_trace_view_row("tr_secret")) + "\n")
    _write_safe_import_manifest(source)
    settings_path = tmp_path / ".kensa" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{")

    assert (
        cli_traces.cmd_traces(
            SimpleNamespace(
                traces_command="get",
                source=str(source),
                trace_id="tr_secret",
                json=True,
            )
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["trace_id"] == "tr_secret"
