from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from kensa import cli, cli_traces


def _write_pyproject(path: Path, dotenv: str | None = None) -> None:
    dotenv_line = f'dotenv = "{dotenv}"\n' if dotenv is not None else ""
    path.write_text(f"[tool.kensa]\n{dotenv_line}")


def _patch_trace_list(monkeypatch: pytest.MonkeyPatch, assertion: Any | None = None) -> None:
    def fake_load_trace_views(_source: Path, **_kwargs: Any) -> list[dict[str, Any]]:
        if assertion is not None:
            assertion()
        return []

    monkeypatch.setattr(cli_traces, "load_trace_views", fake_load_trace_views)


def _assert_env(name: str, value: str) -> None:
    assert os.environ.get(name) == value


def test_cli_loads_pyproject_dotenv_before_subcommand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KENSA_DOTENV", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    dotenv = tmp_path / "config" / "dev.env"
    dotenv.parent.mkdir()
    dotenv.write_text("OPENAI_API_KEY=loaded-from-pyproject\n")
    _write_pyproject(tmp_path / "pyproject.toml", "config/dev.env")
    _patch_trace_list(monkeypatch, lambda: _assert_env("OPENAI_API_KEY", "loaded-from-pyproject"))

    assert cli.main(["traces", "list", "--source", "traces.jsonl"]) == 0


def test_kensa_dotenv_environment_override_wins_over_pyproject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pyproject_dotenv = tmp_path / "pyproject.env"
    pyproject_dotenv.write_text("OPENAI_API_KEY=loaded-from-pyproject\n")
    override_dotenv = tmp_path / "override.env"
    override_dotenv.write_text("OPENAI_API_KEY=loaded-from-override\n")
    _write_pyproject(tmp_path / "pyproject.toml", pyproject_dotenv.name)
    monkeypatch.setenv("KENSA_DOTENV", str(override_dotenv))
    _patch_trace_list(monkeypatch, lambda: _assert_env("OPENAI_API_KEY", "loaded-from-override"))

    assert cli.main(["traces", "list", "--source", "traces.jsonl"]) == 0


def test_cli_dotenv_load_keeps_preexported_openai_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KENSA_DOTENV", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "preexported-value")
    dotenv = tmp_path / "dev.env"
    dotenv.write_text("OPENAI_API_KEY=dotenv-value\n")
    _write_pyproject(tmp_path / "pyproject.toml", dotenv.name)
    _patch_trace_list(monkeypatch, lambda: _assert_env("OPENAI_API_KEY", "preexported-value"))

    assert cli.main(["traces", "list", "--source", "traces.jsonl"]) == 0


def test_cli_without_dotenv_declaration_does_not_read_or_load_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KENSA_DOTENV", raising=False)

    def fail_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        raise AssertionError(f"unexpected file read: {self}")

    def fail_load_dotenv(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("load_dotenv should not be called")

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    monkeypatch.setattr(cli, "load_dotenv", fail_load_dotenv, raising=False)
    _patch_trace_list(monkeypatch)

    assert cli.main(["traces", "list", "--source", "traces.jsonl"]) == 0


@pytest.mark.parametrize(
    "pyproject_text",
    [
        'tool = "not-a-table"\n',
        '[tool]\nkensa = "not-a-table"\n',
        "[tool.kensa]\ndotenv = 123\n",
    ],
)
def test_cli_ignores_unexpected_pyproject_dotenv_shapes(
    pyproject_text: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KENSA_DOTENV", raising=False)
    (tmp_path / "pyproject.toml").write_text(pyproject_text)

    def fail_load_dotenv(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("load_dotenv should not be called")

    monkeypatch.setattr(cli, "load_dotenv", fail_load_dotenv, raising=False)
    _patch_trace_list(monkeypatch)

    assert cli.main(["traces", "list", "--source", "traces.jsonl"]) == 0


def test_missing_declared_dotenv_warns_and_command_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KENSA_DOTENV", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "preexported-value")
    _write_pyproject(tmp_path / "pyproject.toml", "missing.env")
    _patch_trace_list(monkeypatch)

    assert cli.main(["traces", "list", "--source", "traces.jsonl"]) == 0

    captured = capsys.readouterr()
    assert "warning:" in captured.err
    assert "missing.env" in captured.err


def test_loaded_dotenv_secret_does_not_leak_to_cli_json_report_or_traces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KENSA_DOTENV", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    sentinel = "sk-test-sentinel-dotenv-secret-never-print"
    dotenv = tmp_path / "dev.env"
    dotenv.write_text(f"OPENAI_API_KEY={sentinel}\n")
    _write_pyproject(tmp_path / "pyproject.toml", dotenv.name)
    eval_dir = tmp_path / "tests" / "evals"
    eval_dir.mkdir(parents=True)
    (eval_dir / "conftest.py").write_text(
        """import pytest
from kensa import record_llm_call
from kensa.pytest import ConversationResponse


@pytest.fixture
def kensa_run(case):
    class Agent:
        def respond(self, messages):
            with record_llm_call(provider="test", model="test-model"):
                return ConversationResponse(output={"ok": case.input})
    return Agent()
"""
    )
    (eval_dir / "test_dotenv_secret.py").write_text(
        """import os
import pytest
from kensa.pytest import kensa_case


@pytest.mark.kensa(trials=1)
@pytest.mark.parametrize("case", [kensa_case(id="dotenv_secret", input="hello")])
def test_dotenv_secret(case, kensa_run):
    assert os.environ.get("OPENAI_API_KEY") is not None
    result = case.run(kensa_run)
    assert result.output == {"ok": "hello"}
    assert result.trace.llm_turns == 1
"""
    )
    report = tmp_path / "report.json"

    assert cli.main(["eval", "--workers", "1", "--json", "--json-report", str(report)]) == 0

    captured = capsys.readouterr()
    trace_text = "\n".join(
        path.read_text() for path in (tmp_path / ".kensa" / "traces").glob("**/trials.jsonl")
    )
    combined = "\n".join([captured.out, captured.err, report.read_text(), trace_text])
    payload = json.loads(captured.out)
    report_payload = json.loads(report.read_text())
    assert payload["ok"] is True
    assert report_payload["trials"]
    assert sentinel not in combined


def test_setup_skill_allows_declared_dotenv_load_without_secret_value_handling() -> None:
    setup_skill = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "kensa"
        / "skill_templates"
        / "kensa-setup"
        / "SKILL.md"
    ).read_text()

    assert "detect credential presence by name only" in setup_skill
    assert "Never read, print, copy, transform" in setup_skill
    assert '[tool.kensa] dotenv = "<path>"' in setup_skill
    assert "Do not read or edit the dotenv file" in setup_skill
