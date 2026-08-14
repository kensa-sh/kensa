from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from kensa import config as config_module
from kensa.config import (
    KensaConfigError,
    find_pyproject,
    read_dotenv_path,
    read_project_config,
    update_project_config,
)


def test_find_pyproject_uses_nearest_parent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "packages" / "agent"
    nested.mkdir(parents=True)
    root_pyproject = root / "pyproject.toml"
    root_pyproject.write_text("[tool.kensa]\n")

    assert find_pyproject(nested) == root_pyproject
    assert find_pyproject(tmp_path / "missing") is None


def test_read_project_config_allows_unknown_keys(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.kensa]\n"
        'dotenv = "config/dev.env"\n'
        'evidence_source = "langfuse"\n'
        'redaction_model = "large"\n'
        "future_option = true\n"
    )

    config = read_project_config(tmp_path)

    assert config.evidence_source == "langfuse"
    assert config.redaction_model == "large"
    assert read_dotenv_path(tmp_path / "pyproject.toml") == Path("config/dev.env")


@pytest.mark.parametrize(
    "source",
    [
        "{",
        'tool = "not-a-table"\n',
        '[tool]\nkensa = "not-a-table"\n',
        '[tool.kensa]\nevidence_source = "invalid"\n',
        '[tool.kensa]\nredaction_model = "medium"\n',
    ],
)
def test_read_project_config_rejects_invalid_known_configuration(
    source: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(source)

    with pytest.raises(KensaConfigError, match=r"pyproject\.toml"):
        read_project_config(tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        'tool = "not-a-table"\n',
        '[tool]\nkensa = "not-a-table"\n',
        "[tool.kensa]\ndotenv = 123\n",
    ],
)
def test_read_dotenv_path_tolerates_unexpected_shapes(source: str, tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(source)

    assert read_dotenv_path(path) is None


def test_update_project_config_creates_minimal_tool_only_file(tmp_path: Path) -> None:
    result = update_project_config(
        {"evidence_source": "local", "redaction_model": "small"},
        start=tmp_path,
    )

    assert result.created is True
    assert result.changed is True
    assert result.path == tmp_path / "pyproject.toml"
    assert result.path.read_text() == (
        '[tool.kensa]\nevidence_source = "local"\nredaction_model = "small"\n'
    )

    before = result.path.read_bytes()
    repeated = update_project_config(
        {"evidence_source": "local", "redaction_model": "small"},
        start=tmp_path,
    )
    assert repeated.created is False
    assert repeated.changed is False
    assert result.path.read_bytes() == before


def test_update_project_config_with_no_values_writes_nothing(tmp_path: Path) -> None:
    result = update_project_config({}, start=tmp_path)

    assert result.created is False
    assert result.changed is False
    assert not result.path.exists()

    result.path.write_text('[project]\nname = "demo"\n')
    before = result.path.read_bytes()
    repeated = update_project_config({}, start=tmp_path)
    assert repeated.created is False
    assert repeated.changed is False
    assert result.path.read_bytes() == before


def test_update_project_config_preserves_existing_toml_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        "# package comment\n"
        "[project]\n"
        'name = "demo"\n'
        "\n"
        "[tool.kensa] # kensa comment\n"
        'dotenv = "dev.env"\n'
        'evidence_source = "local" # source comment\n'
        "future_option = true\n"
        "\n"
        "[tool.ruff]\n"
        "line-length = 100\n"
    )

    result = update_project_config(
        {"evidence_source": "trace_export", "redaction_model": "large"},
        start=tmp_path,
    )

    assert result.created is False
    assert result.changed is True
    assert path.read_text() == (
        "# package comment\n"
        "[project]\n"
        'name = "demo"\n'
        "\n"
        "[tool.kensa] # kensa comment\n"
        'dotenv = "dev.env"\n'
        'evidence_source = "trace_export" # source comment\n'
        "future_option = true\n"
        'redaction_model = "large"\n'
        "\n"
        "[tool.ruff]\n"
        "line-length = 100\n"
    )


def test_update_project_config_rejects_invalid_existing_file_without_writing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[tool.kensa]\nevidence_source = "invalid"\n')
    before = path.read_bytes()

    with pytest.raises(KensaConfigError, match="invalid Kensa configuration"):
        update_project_config({"redaction_model": "small"}, start=tmp_path)

    assert path.read_bytes() == before


def test_update_project_config_rejects_unsupported_keys(tmp_path: Path) -> None:
    with pytest.raises(KensaConfigError, match="unsupported Kensa configuration keys: unknown"):
        update_project_config({"unknown": "value"}, start=tmp_path)

    assert not (tmp_path / "pyproject.toml").exists()


def test_update_project_config_rejects_invalid_existing_dotenv(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text("[tool.kensa]\ndotenv = 123\n")
    before = path.read_bytes()

    with pytest.raises(KensaConfigError, match="invalid Kensa configuration"):
        update_project_config({"redaction_model": "small"}, start=tmp_path)

    assert path.read_bytes() == before


def test_update_project_config_preserves_comments_after_escaped_strings(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[tool.kensa]\ndotenv = "dev\\"#file.env" # keep this\n')

    update_project_config({"dotenv": "new.env"}, start=tmp_path)

    assert path.read_text() == '[tool.kensa]\ndotenv = "new.env" # keep this\n'


@pytest.mark.parametrize(
    "source",
    [
        '[tool]\nkensa = { evidence_source = "local" }\n',
        '[tool]\nkensa.evidence_source = "local"\n',
    ],
)
def test_update_project_config_rejects_non_section_kensa_values_without_writing(
    source: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(source)
    before = path.read_bytes()

    with pytest.raises(KensaConfigError, match=r"use a \[tool\.kensa\] table"):
        update_project_config({"redaction_model": "small"}, start=tmp_path)

    assert path.read_bytes() == before


def test_update_project_config_leaves_matching_inline_kensa_table_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[tool]\nkensa = { evidence_source = "local" }\n')
    before = path.read_bytes()

    result = update_project_config({"evidence_source": "local"}, start=tmp_path)

    assert result.changed is False
    assert path.read_bytes() == before


def test_update_project_config_preserves_crlf_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_bytes(
        b'[tool.kensa]\r\nevidence_source = "local"\r\n\r\n[tool.ruff]\r\nline-length = 100\r\n'
    )

    update_project_config({"evidence_source": "trace_export"}, start=tmp_path)

    assert path.read_bytes() == (
        b'[tool.kensa]\r\nevidence_source = "trace_export"\r\n\r\n'
        b"[tool.ruff]\r\nline-length = 100\r\n"
    )


def test_update_project_config_rejects_cr_only_toml_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_bytes(b'[tool.kensa]\revidence_source = "local"\r')
    before = path.read_bytes()

    with pytest.raises(KensaConfigError, match="invalid TOML"):
        update_project_config({"evidence_source": "trace_export"}, start=tmp_path)

    assert path.read_bytes() == before


def test_update_project_config_ignores_header_shaped_array_and_string_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        "[tool.kensa]\n"
        'future_text = """\n'
        'escaped \\" quote\n'
        "[tool.not_a_table]\n"
        '"""\n'
        "future_array = [\n"
        '  ["a"]\n'
        "]\n"
        "[tool.ruff]\n"
        "line-length = 100\n"
    )

    update_project_config({"redaction_model": "small"}, start=tmp_path)

    assert path.read_text() == (
        "[tool.kensa]\n"
        'future_text = """\n'
        'escaped \\" quote\n'
        "[tool.not_a_table]\n"
        '"""\n'
        "future_array = [\n"
        '  ["a"]\n'
        "]\n"
        'redaction_model = "small"\n'
        "[tool.ruff]\n"
        "line-length = 100\n"
    )


def test_update_project_config_updates_quoted_known_key(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[tool.kensa]\n"dotenv" = ".env"\n')

    update_project_config({"dotenv": "config/dev.env"}, start=tmp_path)

    assert path.read_text() == '[tool.kensa]\n"dotenv" = "config/dev.env"\n'


def test_update_project_config_preserves_unicode_line_separator(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\ndescription = "a\u2028b"\n\n[tool.kensa]\nevidence_source = "local"\n'
    )

    update_project_config({"evidence_source": "trace_export"}, start=tmp_path)

    assert 'description = "a\u2028b"' in path.read_text()
    assert read_project_config(tmp_path).evidence_source == "trace_export"


def test_update_project_config_ignores_kensa_header_inside_multiline_string(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\ndescription = """not config\n[tool.kensa]\nstill not config\n"""\n')

    update_project_config({"redaction_model": "small"}, start=tmp_path)

    data = tomllib.loads(path.read_text())
    assert "redaction_model" not in data["project"]
    assert data["tool"]["kensa"]["redaction_model"] == "small"


def test_update_project_config_extends_implicit_kensa_parent_table(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text("[tool.kensa.sub]\nfuture = true\n")

    update_project_config({"redaction_model": "small"}, start=tmp_path)

    data = tomllib.loads(path.read_text())
    assert data["tool"]["kensa"] == {
        "redaction_model": "small",
        "sub": {"future": True},
    }


@pytest.mark.parametrize("rendered", ["{", '[project]\nname = "demo"\n'])
def test_update_project_config_validates_rendered_output_before_writing(
    rendered: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "demo"\n')
    before = path.read_bytes()
    monkeypatch.setattr(config_module, "_update_source", lambda source, updates: rendered)

    with pytest.raises(KensaConfigError, match="could not safely update"):
        update_project_config({"redaction_model": "small"}, start=tmp_path)

    assert path.read_bytes() == before
