from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from kensa import cli_inspect
from kensa.cli import main
from kensa.models import ExpectedCurrentBehavior, InspectIdea, InspectQueue, InspectStatus

INSPECT_DIR = Path(".kensa/inspect")


def _idea_payload(item_id: str = "tool-loop-on-empty-results", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": item_id,
        "trace_ids": ["tr_abc123"],
        "source": "langfuse https://cloud.langfuse.com/trace/tr_abc123",
        "status": "pending",
        "failure_pattern": "agent loops the search tool on empty results",
        "expected_outcome": "agent stops retrying after two empty results",
        "expected_current_behavior": "fail",
    }
    payload.update(overrides)
    return payload


def _write_queue(root: Path, name: str, items: list[dict[str, Any]]) -> Path:
    queue_dir = root / INSPECT_DIR
    queue_dir.mkdir(parents=True, exist_ok=True)
    path = queue_dir / name
    path.write_text(yaml.safe_dump({"schema_version": "kensa.inspect.v1", "items": items}))
    return path


def _lint_args(json_output: bool = False) -> SimpleNamespace:
    return SimpleNamespace(inspect_command="lint", json=json_output)


def _list_args(status: str | None = None, json_output: bool = False) -> SimpleNamespace:
    return SimpleNamespace(inspect_command="list", status=status, json=json_output)


def test_inspect_idea_parses_and_applies_defaults() -> None:
    idea = InspectIdea.model_validate(_idea_payload(failure_pattern="  padded evidence  "))

    assert idea.status is InspectStatus.PENDING
    assert idea.expected_current_behavior is ExpectedCurrentBehavior.FAIL
    assert idea.failure_pattern == "padded evidence"
    assert idea.proposed_checks == []
    assert idea.case_shape is None
    assert idea.risks is None


def test_inspect_idea_rejects_legacy_approval_field() -> None:
    with pytest.raises(ValidationError, match="approval"):
        InspectIdea.model_validate(_idea_payload(approval="approved"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "maybe"),
        ("expected_current_behavior", "unknown"),
        ("id", "Bad_Slug"),
        ("id", "x" * 65),
        ("failure_pattern", ""),
        ("failure_pattern", "   "),
        ("expected_outcome", ""),
        ("source", ""),
        ("source", " "),
        ("trace_ids", []),
    ],
)
def test_inspect_idea_rejects_invalid_field_values(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        InspectIdea.model_validate(_idea_payload(**{field: value}))


def test_inspect_idea_rejects_blank_trace_id_entries() -> None:
    with pytest.raises(ValidationError, match="trace_ids entries must be non-empty"):
        InspectIdea.model_validate(_idea_payload(trace_ids=["tr_abc123", "  "]))


def test_inspect_queue_rejects_wrong_schema_version() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        InspectQueue.model_validate(
            {"schema_version": "kensa.inspect.v2", "items": [_idea_payload()]}
        )


def test_inspect_queue_rejects_duplicate_item_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate inspect item ids: dup-idea"):
        InspectQueue.model_validate(
            {
                "schema_version": "kensa.inspect.v1",
                "items": [_idea_payload("dup-idea"), _idea_payload("dup-idea")],
            }
        )


def test_load_inspect_queues_returns_empty_when_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    loaded, errors, warnings = cli_inspect.load_inspect_queues()

    assert loaded == []
    assert errors == []
    assert warnings == []


def test_load_inspect_queues_collects_files_errors_and_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_queue(tmp_path, "good.yaml", [_idea_payload("good-idea")])
    _write_queue(tmp_path, "also-good.yml", [_idea_payload("other-idea")])
    (tmp_path / INSPECT_DIR / "legacy.md").write_text("## Eval idea\n- approval: pending\n")
    (tmp_path / INSPECT_DIR / "broken.yaml").write_text("items: [unclosed\n")
    (tmp_path / INSPECT_DIR / "invalid.yaml").write_text("items: {}\n")
    (tmp_path / INSPECT_DIR / "directory.yaml").mkdir()

    loaded, errors, warnings = cli_inspect.load_inspect_queues()

    assert [entry.path.name for entry in loaded] == ["also-good.yml", "good.yaml"]
    assert len(errors) == 3
    assert "invalid YAML" in errors[0]
    assert "broken.yaml" in errors[0]
    assert "invalid YAML" in errors[1]
    assert "directory.yaml" in errors[1]
    assert "invalid.yaml" in errors[2]
    assert warnings == [f"legacy markdown queue ignored: {INSPECT_DIR / 'legacy.md'}"]


def test_load_inspect_queues_errors_on_duplicate_ids_across_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_queue(tmp_path, "first.yaml", [_idea_payload("shared-idea")])
    _write_queue(tmp_path, "second.yaml", [_idea_payload("shared-idea")])

    loaded, errors, _warnings = cli_inspect.load_inspect_queues()

    assert len(loaded) == 2
    assert errors == [
        "duplicate inspect item id across files: shared-idea "
        f"({INSPECT_DIR / 'first.yaml'}, {INSPECT_DIR / 'second.yaml'})"
    ]


def test_lint_warns_when_latest_import_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_queue(tmp_path, "queue.yaml", [_idea_payload()])

    assert cli_inspect.cmd_inspect(_lint_args(json_output=True)) == 0

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["data"]["item_count"] == 1
    assert any("could not verify trace ids" in warning for warning in envelope["warnings"])


def test_lint_warns_on_trace_ids_missing_from_latest_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_queue(tmp_path, "queue.yaml", [_idea_payload(trace_ids=["tr_known", "tr_missing"])])
    monkeypatch.setattr(cli_inspect, "resolve_trace_view_source", lambda source: tmp_path)
    monkeypatch.setattr(
        cli_inspect,
        "load_trace_views",
        lambda source, **kwargs: [{"id": "tr_known"}],
    )

    assert cli_inspect.cmd_inspect(_lint_args(json_output=True)) == 0

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["warnings"] == ["trace id not found in latest import: tr_missing"]


def test_lint_skips_trace_verification_without_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_queue(tmp_path, "queue.yaml", [])

    assert cli_inspect.cmd_inspect(_lint_args(json_output=True)) == 0

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["warnings"] == []
    assert envelope["data"]["item_count"] == 0


def test_lint_terminal_reports_files_warnings_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_queue(tmp_path, "queue.yaml", [_idea_payload()])
    (tmp_path / INSPECT_DIR / "legacy.md").write_text("stale\n")
    (tmp_path / INSPECT_DIR / "broken.yaml").write_text("items: [unclosed\n")

    assert cli_inspect.cmd_inspect(_lint_args()) == 1

    captured = capsys.readouterr()
    assert "queue.yaml: 1 item(s)" in captured.out
    assert "legacy markdown queue ignored" in captured.out
    assert "invalid YAML" in captured.err
    assert "Fix the reported queue files" in captured.out


def test_lint_json_failure_reports_errors_and_next_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / INSPECT_DIR).mkdir(parents=True)
    (tmp_path / INSPECT_DIR / "invalid.yaml").write_text("schema_version: nope\n")

    assert cli_inspect.cmd_inspect(_lint_args(json_output=True)) == 1

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["summary"] == "Inspect queue validation failed."
    assert envelope["errors"]
    assert envelope["next_steps"] == ["Fix the reported queue files and rerun kensa inspect lint."]


def test_list_json_returns_items_with_file_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_queue(
        tmp_path,
        "queue.yaml",
        [_idea_payload("pending-idea"), _idea_payload("approved-idea", status="approved")],
    )

    assert cli_inspect.cmd_inspect(_list_args(json_output=True)) == 0

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["data"]["count"] == 2
    first = envelope["data"]["items"][0]
    assert first["file"] == str(INSPECT_DIR / "queue.yaml")
    assert first["id"] == "pending-idea"
    assert first["status"] == "pending"
    assert first["expected_current_behavior"] == "fail"


def test_list_filters_by_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_queue(
        tmp_path,
        "queue.yaml",
        [
            _idea_payload("pending-idea"),
            _idea_payload("approved-idea", status="approved"),
            _idea_payload("generated-idea", status="generated"),
        ],
    )

    assert cli_inspect.cmd_inspect(_list_args(status="approved", json_output=True)) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in envelope["data"]["items"]] == ["approved-idea"]

    assert cli_inspect.cmd_inspect(_list_args(status="generated", json_output=True)) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in envelope["data"]["items"]] == ["generated-idea"]


def test_list_terminal_prints_ids_and_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_queue(
        tmp_path,
        "queue.yaml",
        [_idea_payload("pending-idea"), _idea_payload("approved-idea", status="approved")],
    )

    assert cli_inspect.cmd_inspect(_list_args()) == 0

    assert capsys.readouterr().out == "pending-idea pending\napproved-idea approved\n"


def test_list_reports_invalid_queue_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_queue(tmp_path, "queue.yaml", [_idea_payload()])
    (tmp_path / INSPECT_DIR / "broken.yaml").write_text("items: [unclosed\n")

    assert cli_inspect.cmd_inspect(_list_args()) == 1

    captured = capsys.readouterr()
    assert "pending" in captured.out
    assert "invalid YAML" in captured.err
    assert "Fix the reported queue files" in captured.out

    assert cli_inspect.cmd_inspect(_list_args(json_output=True)) == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["errors"]


def test_cmd_inspect_rejects_unknown_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert cli_inspect.cmd_inspect(SimpleNamespace(inspect_command="nope", json=False)) == 2
    assert cli_inspect.cmd_inspect(SimpleNamespace(inspect_command="nope", json=True)) == 2

    captured = capsys.readouterr()
    assert "unknown inspect command: nope" in captured.err
    envelope = json.loads(captured.out)
    assert envelope["exit_code"] == 2
    assert envelope["errors"] == ["unknown inspect command: nope"]


def test_main_wires_inspect_list_and_lint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_queue(tmp_path, "queue.yaml", [_idea_payload("approved-idea", status="approved")])
    monkeypatch.setattr(cli_inspect, "resolve_trace_view_source", lambda source: tmp_path)
    monkeypatch.setattr(
        cli_inspect,
        "load_trace_views",
        lambda source, **kwargs: [{"id": "tr_abc123"}],
    )

    assert main(["inspect", "lint", "--json"]) == 0
    lint_envelope = json.loads(capsys.readouterr().out)
    assert lint_envelope["command"] == "inspect lint"
    assert lint_envelope["warnings"] == []

    assert main(["inspect", "list", "--status", "approved", "--json"]) == 0
    list_envelope = json.loads(capsys.readouterr().out)
    assert list_envelope["command"] == "inspect list"
    assert [item["id"] for item in list_envelope["data"]["items"]] == ["approved-idea"]
