from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

import scripts.changelog as changelog_module
from scripts.changelog import (
    build_release_section,
    check_docs_release,
    extract_release_notes,
    prepend_release,
)

CHANGELOG_PREAMBLE = """# Changelog

Release notes for Kensa. Full notes are available on [GitHub Releases](https://github.com/kensa-sh/kensa/releases).

"""

GENERATED_NOTES = (
    "<!-- Release notes generated using configuration in "
    ".github/release.yml at main -->\n"
    """

## What's Changed
### Features
* feat: add release previews by @satyaborg in https://github.com/kensa-sh/kensa/pull/123

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v1.2.2...v1.2.3
"""
)


def test_build_release_section_normalizes_generated_notes() -> None:
    section = build_release_section("1.2.3", GENERATED_NOTES)

    assert (
        section
        == """## 1.2.3
### Features
* feat: add release previews by @satyaborg in https://github.com/kensa-sh/kensa/pull/123

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v1.2.2...v1.2.3
"""
    )


def test_build_release_section_handles_generated_notes_without_changes() -> None:
    generated_notes = (
        "<!-- Release notes generated using configuration in "
        ".github/release.yml at main -->\n"
        """


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v1.2.2...v1.2.3
"""
    )

    assert (
        build_release_section("1.2.3", generated_notes)
        == """## 1.2.3

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v1.2.2...v1.2.3
"""
    )


@pytest.mark.parametrize("version", ["v1.2.3", "1.2", "1.2.3-rc.1", "01.2.3"])
def test_build_release_section_rejects_invalid_versions(version: str) -> None:
    with pytest.raises(ValueError, match=f"invalid release version: {re.escape(version)}"):
        build_release_section(version, GENERATED_NOTES)


def test_build_release_section_rejects_empty_generated_notes() -> None:
    with pytest.raises(ValueError, match="generated release notes are empty"):
        build_release_section("1.2.3", "\n")


def test_build_release_section_rejects_malformed_generation_comment() -> None:
    generated_notes = "<!-- Release notes generated using .github/release.yml\n\nNotes"

    with pytest.raises(ValueError, match="generated release-notes comment is malformed"):
        build_release_section("1.2.3", generated_notes)


def test_prepend_release_round_trips_exact_release_notes() -> None:
    changelog = f"{CHANGELOG_PREAMBLE}## 1.2.2\n\nPrevious release.\n"

    updated = prepend_release(changelog, "1.2.3", GENERATED_NOTES)

    assert updated.startswith(f"{CHANGELOG_PREAMBLE}## 1.2.3\n")
    assert updated.endswith("## 1.2.2\n\nPrevious release.\n")
    assert (
        extract_release_notes(updated, "1.2.3")
        == """## What's Changed
### Features
* feat: add release previews by @satyaborg in https://github.com/kensa-sh/kensa/pull/123

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v1.2.2...v1.2.3
"""
    )


def test_prepend_release_rejects_duplicate_version() -> None:
    changelog = f"{CHANGELOG_PREAMBLE}## 1.2.3\n\nExisting release.\n"

    with pytest.raises(ValueError, match=r"release 1\.2\.3 already exists"):
        prepend_release(changelog, "1.2.3", GENERATED_NOTES)


def test_extract_release_notes_rejects_missing_version() -> None:
    with pytest.raises(ValueError, match=r"release 1\.2\.3 is missing"):
        extract_release_notes(f"{CHANGELOG_PREAMBLE}## 1.2.2\n", "1.2.3")


def test_check_docs_release_accepts_independent_product_notes() -> None:
    docs_changelog = "---\ntitle: Changelog\n---\n\n## 1.2.3\n\nProduct release notes.\n"

    check_docs_release("1.2.3", docs_changelog)


def test_check_docs_release_rejects_missing_version() -> None:
    docs_changelog = "---\ntitle: Changelog\n---\n\n## 1.2.2\n\nOlder notes.\n"

    with pytest.raises(ValueError, match=r"docs/changelog\.mdx is missing release 1\.2\.3"):
        check_docs_release("1.2.3", docs_changelog)


def test_main_add_updates_only_generated_changelog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changelog_path = tmp_path / "CHANGELOG.md"
    docs_changelog_path = tmp_path / "docs" / "changelog.mdx"
    docs_changelog_path.parent.mkdir()
    changelog_path.write_text(f"{CHANGELOG_PREAMBLE}## 1.2.2\n\nOlder release.\n")
    docs_changelog_path.write_text("independent product notes\n")
    monkeypatch.setattr(changelog_module, "CHANGELOG_PATH", changelog_path)
    monkeypatch.setattr(changelog_module, "DOCS_CHANGELOG_PATH", docs_changelog_path)
    monkeypatch.setattr(changelog_module.sys, "stdin", io.StringIO(GENERATED_NOTES))

    assert changelog_module.main(["add", "1.2.3"]) == 0
    assert "## 1.2.3" in changelog_path.read_text()
    assert docs_changelog_path.read_text() == "independent product notes\n"


def test_main_check_docs_reports_missing_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    docs_changelog_path = tmp_path / "changelog.mdx"
    docs_changelog_path.write_text("## 1.2.2\n")
    monkeypatch.setattr(changelog_module, "DOCS_CHANGELOG_PATH", docs_changelog_path)

    assert changelog_module.main(["check-docs", "1.2.3"]) == 1
    assert capsys.readouterr().err == "error: docs/changelog.mdx is missing release 1.2.3\n"


def test_main_reports_changelog_validation_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    changelog_path = tmp_path / "CHANGELOG.md"
    changelog_path.write_text(f"{CHANGELOG_PREAMBLE}## 1.2.3\n\nRelease notes.\n")
    monkeypatch.setattr(changelog_module, "CHANGELOG_PATH", changelog_path)

    assert changelog_module.main(["notes", "v1.2.3"]) == 1
    assert capsys.readouterr().err == "error: invalid release version: v1.2.3\n"


def test_main_reports_changelog_read_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_changelog = tmp_path / "CHANGELOG.md"
    monkeypatch.setattr(changelog_module, "CHANGELOG_PATH", missing_changelog)

    assert changelog_module.main(["notes", "1.2.3"]) == 1
    assert capsys.readouterr().err.startswith("error: [Errno 2] No such file or directory:")
