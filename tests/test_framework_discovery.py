from __future__ import annotations

import ast
import hashlib
import json
import runpy
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FunctionType
from typing import Any, cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT / "src" / "kensa" / "skill_templates" / "kensa-setup"
DETECTOR_PATH = SKILL_ROOT / "scripts" / "detect_frameworks.py"
REGISTRY_PATH = SKILL_ROOT / "assets" / "frameworks.json"

IGNORED_DIRECTORIES = {
    ".direnv",
    ".eggs",
    ".git",
    ".hg",
    ".idea",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "site-packages",
    "venv",
}


@pytest.fixture
def detector() -> dict[str, Any]:
    return runpy.run_path(str(DETECTOR_PATH))


def _write(root: Path, relative_path: str, content: str) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _invoke(
    detector: dict[str, Any],
    root: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    main = cast(Callable[[list[str]], int], detector["main"])
    status = main(["--root", str(root)])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def _scan(
    detector: dict[str, Any],
    root: Path,
    capsys: pytest.CaptureFixture[str],
) -> dict[str, Any]:
    status, stdout, stderr = _invoke(detector, root, capsys)
    assert status == 0
    assert stderr == ""
    return cast(dict[str, Any], json.loads(stdout))


def _snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): (
            f"symlink:{path.readlink()}"
            if path.is_symlink()
            else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for path in root.rglob("*")
        if path.is_symlink() or path.is_file()
    }


def test_registry_preserves_compact_framework_identity_contract() -> None:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    assert set(registry) == {"entries"}
    entries = registry["entries"]
    assert len(entries) == 47
    assert sum(entry["kind"] == "framework" for entry in entries) == 40
    assert sum(entry["kind"] == "client" for entry in entries) == 7
    assert [entry["id"] for entry in entries] == sorted(entry["id"] for entry in entries)
    assert len({entry["id"] for entry in entries}) == len(entries)
    for entry in entries:
        assert set(entry) == {
            "id",
            "kind",
            "distributions",
            "import_roots",
            "documentation",
        }
        assert entry["kind"] in {"framework", "client"}
        assert entry["distributions"] == sorted(set(entry["distributions"]))
        assert entry["import_roots"] == sorted(set(entry["import_roots"]))
        assert entry["documentation"].startswith("https://")

    by_id = {entry["id"]: entry for entry in entries}
    assert by_id["agno"]["distributions"] == ["agno", "phidata"]
    assert by_id["autogen"]["distributions"] == [
        "autogen-agentchat",
        "autogen-core",
        "autogen-ext",
        "pyautogen",
    ]
    assert by_id["google-genai"]["distributions"] == [
        "google-genai",
        "google-generativeai",
    ]


def test_absolute_imports_emit_grouped_sorted_candidates(
    tmp_path: Path,
    detector: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(
        tmp_path,
        "z.py",
        "from google import adk\nimport crewai, autogen\nfrom . import openai\n",
    )
    _write(
        tmp_path,
        "a.py",
        "\nfrom langgraph.graph import StateGraph\nimport crewai as crew\nimport unknown_package\n",
    )
    _write(tmp_path, "m.py", "import autogen.agentchat as agentchat\n")

    document = _scan(detector, tmp_path, capsys)

    assert document["parse_errors"] == []
    assert [match["id"] for match in document["matches"]] == [
        "ag2",
        "autogen",
        "crewai",
        "google-adk",
        "langgraph",
    ]
    matches = {match["id"]: match for match in document["matches"]}
    assert matches["ag2"]["imports"] == [
        {"module": "autogen.agentchat", "path": "m.py", "line": 1},
        {"module": "autogen", "path": "z.py", "line": 2},
    ]
    assert matches["autogen"]["imports"] == matches["ag2"]["imports"]
    assert matches["crewai"]["imports"] == [
        {"module": "crewai", "path": "a.py", "line": 3},
        {"module": "crewai", "path": "z.py", "line": 2},
    ]
    assert matches["google-adk"]["imports"] == [{"module": "google.adk", "path": "z.py", "line": 1}]
    assert matches["langgraph"]["imports"] == [
        {"module": "langgraph.graph", "path": "a.py", "line": 2}
    ]
    for match in document["matches"]:
        assert match["distributions"] == sorted(match["distributions"])
        assert match["imports"] == sorted(
            match["imports"], key=lambda item: (item["path"], item["line"], item["module"])
        )
        assert set(match) == {"id", "kind", "distributions", "documentation", "imports"}


def test_equivalent_trees_render_byte_identically_with_unstable_enumeration(
    tmp_path: Path,
    detector: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = tmp_path / "first-parent" / "repo"
    second = tmp_path / "other-parent" / "repo"
    for relative_path, content in (
        ("b/module.py", "import openai\n"),
        ("a/module.py", "import anthropic\n"),
        ("broken/z.py", "def invalid(:\n"),
        ("broken/a.py", b"\xff"),
    ):
        if isinstance(content, bytes):
            path = first / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        else:
            _write(first, relative_path, content)
    for relative_path in reversed(("b/module.py", "a/module.py", "broken/z.py")):
        _write(second, relative_path, (first / relative_path).read_text(encoding="utf-8"))
    invalid = second / "broken/a.py"
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_bytes(b"\xff")

    status, first_stdout, first_stderr = _invoke(detector, first, capsys)
    assert status == 0
    assert first_stderr == ""

    original_iterdir = Path.iterdir

    def reversed_iterdir(path: Path) -> Iterator[Path]:
        return iter(reversed(list(original_iterdir(path))))

    monkeypatch.setattr(Path, "iterdir", reversed_iterdir)
    status, second_stdout, second_stderr = _invoke(detector, second, capsys)

    assert status == 0
    assert second_stderr == ""
    assert second_stdout == first_stdout
    assert json.loads(first_stdout)["parse_errors"] == ["broken/a.py", "broken/z.py"]


def test_scan_is_read_only_and_excludes_ignored_and_symlinked_paths(
    tmp_path: Path,
    detector: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    _write(repo, "app.py", "import crewai\n")
    for directory in IGNORED_DIRECTORIES:
        _write(repo, f"{directory}/hidden.py", "import openai\n")
    unreadable_directory = repo / "unreadable"
    _write(unreadable_directory, "hidden.py", "import anthropic\n")
    outside = tmp_path / "outside"
    outside_source = _write(outside, "outside.py", "import langgraph\n")
    (repo / "linked-directory").symlink_to(outside, target_is_directory=True)
    (repo / "linked.py").symlink_to(outside_source)
    before = _snapshot(repo)
    opened: list[Path] = []
    original_read_text = Path.read_text
    original_iterdir = Path.iterdir

    def recording_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(path)
        return original_read_text(path, *args, **kwargs)

    def guarded_iterdir(path: Path) -> Iterator[Path]:
        if path == unreadable_directory:
            raise PermissionError(13, "do not leak", str(path))
        return original_iterdir(path)

    monkeypatch.setattr(Path, "read_text", recording_read_text)
    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

    document = _scan(detector, repo, capsys)

    assert [match["id"] for match in document["matches"]] == ["crewai"]
    assert document["parse_errors"] == []
    assert _snapshot(repo) == before
    assert [path for path in opened if path.is_relative_to(repo)] == [repo / "app.py"]
    assert detector["IGNORED_DIRECTORIES"] == IGNORED_DIRECTORIES


def test_source_failures_are_path_safe_sorted_and_do_not_stop_scanning(
    tmp_path: Path,
    detector: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(tmp_path, "valid.py", "import openai\n")
    _write(tmp_path, "syntax.py", "SECRET_SOURCE_EXCERPT =\n")
    undecodable = tmp_path / "decode.py"
    undecodable.write_bytes(b"\xff")
    denied = _write(tmp_path, "denied.py", "import anthropic\n")
    original_read_text = Path.read_text

    def failing_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == denied:
            raise PermissionError(13, "TOP_SECRET_EXCEPTION", str(path))
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)

    status, stdout, stderr = _invoke(detector, tmp_path, capsys)

    assert status == 0
    assert stderr == ""
    assert json.loads(stdout) == {
        "matches": [
            {
                "id": "openai",
                "kind": "client",
                "distributions": ["openai"],
                "documentation": "https://platform.openai.com/docs/api-reference",
                "imports": [{"module": "openai", "path": "valid.py", "line": 1}],
            }
        ],
        "parse_errors": ["decode.py", "denied.py", "syntax.py"],
    }
    assert str(tmp_path) not in stdout
    assert "SECRET_SOURCE_EXCERPT" not in stdout
    assert "TOP_SECRET_EXCEPTION" not in stdout


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_cli_rejects_invalid_roots_without_json_or_path_leaks(
    tmp_path: Path,
    detector: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    root_kind: str,
) -> None:
    root = tmp_path / root_kind
    if root_kind == "file":
        root.write_text("not a directory", encoding="utf-8")

    status, stdout, stderr = _invoke(detector, root, capsys)

    assert status == 2
    assert stdout == ""
    assert stderr == "error: root is not a directory\n"
    assert str(tmp_path) not in stderr


@pytest.mark.parametrize(
    "registry_content",
    [
        None,
        "{",
        "[]",
        '{"entries": {}}',
        '{"entries": [{"id": "incomplete"}]}',
        (
            '{"entries": [{"id": "", "kind": "client", "distributions": [], '
            '"import_roots": ["x"], "documentation": "https://example.com"}]}'
        ),
        (
            '{"entries": ['
            '{"id": "x", "kind": "client", "distributions": [], '
            '"import_roots": ["x"], "documentation": "https://example.com"},'
            '{"id": "x", "kind": "client", "distributions": [], '
            '"import_roots": ["x"], "documentation": "https://example.com"}'
            "]}"
        ),
    ],
)
def test_cli_rejects_unavailable_malformed_or_invalid_registries_without_partial_json(
    tmp_path: Path,
    detector: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    registry_content: str | None,
) -> None:
    root = tmp_path / "repo"
    _write(root, "app.py", "import openai\n")
    registry = tmp_path / "private" / "frameworks.json"
    if registry_content is not None:
        _write(tmp_path, "private/frameworks.json", registry_content)
    main = cast(FunctionType, detector["main"])
    monkeypatch.setitem(main.__globals__, "REGISTRY_PATH", registry)

    status, stdout, stderr = _invoke(detector, root, capsys)

    assert status == 1
    assert stdout == ""
    assert stderr == "error: framework registry is unavailable or invalid\n"
    assert str(tmp_path) not in stderr


def test_cli_sanitizes_registry_read_errors(
    tmp_path: Path,
    detector: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = tmp_path / "private" / "frameworks.json"
    _write(tmp_path, "private/frameworks.json", '{"entries": []}')
    main = cast(FunctionType, detector["main"])
    monkeypatch.setitem(main.__globals__, "REGISTRY_PATH", registry)
    original_read_text = Path.read_text

    def failing_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == registry:
            raise PermissionError(13, "SECRET_REGISTRY_ERROR", str(path))
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)

    status, stdout, stderr = _invoke(detector, tmp_path, capsys)

    assert status == 1
    assert stdout == ""
    assert stderr == "error: framework registry is unavailable or invalid\n"
    assert str(tmp_path) not in stderr
    assert "SECRET_REGISTRY_ERROR" not in stderr


def test_detector_entrypoint_prints_json_and_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(tmp_path, "app.py", "import openai\n")
    monkeypatch.setattr(sys, "argv", ["detect_frameworks.py", "--root", str(tmp_path)])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(DETECTOR_PATH), run_name="__main__")

    assert excinfo.value.code == 0
    assert json.loads(capsys.readouterr().out)["matches"][0]["id"] == "openai"


def test_detector_source_has_only_read_only_stdlib_capabilities() -> None:
    source = DETECTOR_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None
    )

    assert imported_roots <= {
        "__future__",
        "argparse",
        "ast",
        "json",
        "sys",
        "pathlib",
        "typing",
    }
    for banned in (
        "subprocess",
        "socket",
        "urllib",
        "requests",
        "httpx",
        "importlib",
        "__import__",
        "eval(",
        "exec(",
        "write_text",
        "write_bytes",
    ):
        assert banned not in source


def test_setup_skill_requires_confirmation_before_lookup_and_preserves_fallback() -> None:
    skill = " ".join((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8").split())

    assert "scripts/detect_frameworks.py --root ." in skill
    assert "Every detector match is an unconfirmed candidate" in skill
    assert "Confirm candidates against actual application control flow before" in skill
    assert "Resolve versions only for confirmed candidates" in skill
    assert "official documentation only for confirmed candidates" in skill
    assert "If no candidate is confirmed" in skill
    assert "generic forward and backward source tracing" in skill
    assert "never selects a production target" in skill
