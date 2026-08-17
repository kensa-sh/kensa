from __future__ import annotations

import re
from pathlib import Path

import pytest

import scripts.changelog as changelog_module
from scripts.changelog import (
    build_changelog,
    build_release_section,
    check_docs_release,
    extract_release_notes,
)

CHANGELOG_PREAMBLE = """# Changelog

<!-- Generated from Git history by scripts/release.sh. Do not edit manually. -->

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


def test_build_changelog_sorts_and_normalizes_generated_release_history() -> None:
    previous_notes = GENERATED_NOTES.replace("v1.2.2...v1.2.3", "v1.2.1...v1.2.2")

    changelog = build_changelog([("1.2.2", previous_notes), ("1.2.3", GENERATED_NOTES)])

    assert changelog.startswith(f"{CHANGELOG_PREAMBLE}## 1.2.3\n")
    assert changelog.index("## 1.2.3") < changelog.index("## 1.2.2")
    assert "Previous release." not in changelog
    assert (
        extract_release_notes(changelog, "1.2.3")
        == """## What's Changed
### Features
* feat: add release previews by @satyaborg in https://github.com/kensa-sh/kensa/pull/123

**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v1.2.2...v1.2.3
"""
    )


def test_build_changelog_rejects_duplicate_versions() -> None:
    with pytest.raises(ValueError, match=r"duplicate generated release: 1\.2\.3"):
        build_changelog([("1.2.3", GENERATED_NOTES), ("1.2.3", GENERATED_NOTES)])


def test_extract_release_notes_rejects_missing_version() -> None:
    with pytest.raises(ValueError, match=r"release 1\.2\.3 is missing"):
        extract_release_notes(f"{CHANGELOG_PREAMBLE}## 1.2.2\n", "1.2.3")


def test_extract_release_notes_preserves_generated_second_level_headings() -> None:
    changelog = f"""{CHANGELOG_PREAMBLE}## 1.2.3

## New Contributors
* @contributor made their first contribution

**Full Changelog**: https://example.com/compare/v1.2.2...v1.2.3

## 1.2.2

Previous release.
"""

    assert (
        extract_release_notes(changelog, "1.2.3")
        == """## What's Changed

## New Contributors
* @contributor made their first contribution

**Full Changelog**: https://example.com/compare/v1.2.2...v1.2.3
"""
    )


def test_check_docs_release_accepts_independent_product_notes() -> None:
    docs_changelog = "---\ntitle: Changelog\n---\n\n## 1.2.3\n\nProduct release notes.\n"

    check_docs_release("1.2.3", docs_changelog)


def test_check_docs_release_rejects_missing_version() -> None:
    docs_changelog = "---\ntitle: Changelog\n---\n\n## 1.2.2\n\nOlder notes.\n"

    with pytest.raises(ValueError, match=r"docs/changelog\.mdx is missing release 1\.2\.3"):
        check_docs_release("1.2.3", docs_changelog)


def test_main_rebuild_updates_only_generated_changelog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changelog_path = tmp_path / "CHANGELOG.md"
    docs_changelog_path = tmp_path / "docs" / "changelog.mdx"
    notes_path = tmp_path / "generated"
    docs_changelog_path.parent.mkdir()
    notes_path.mkdir()
    changelog_path.write_text(f"{CHANGELOG_PREAMBLE}## 1.2.2\n\nCopied product prose.\n")
    docs_changelog_path.write_text("independent product notes\n")
    (notes_path / "1.2.3.md").write_text(GENERATED_NOTES)
    monkeypatch.setattr(changelog_module, "CHANGELOG_PATH", changelog_path)
    monkeypatch.setattr(changelog_module, "DOCS_CHANGELOG_PATH", docs_changelog_path)

    assert changelog_module.main(["rebuild", str(notes_path)]) == 0
    assert "## 1.2.3" in changelog_path.read_text()
    assert "Copied product prose." not in changelog_path.read_text()
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
