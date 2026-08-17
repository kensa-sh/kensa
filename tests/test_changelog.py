from __future__ import annotations

from pathlib import Path

import pytest

import scripts.changelog as changelog_module
from scripts.changelog import check_docs_release, extract_release_notes

CHANGELOG_PREAMBLE = """# Changelog

<!-- Generated from Git history by git-cliff. Do not edit manually. -->

Release notes for Kensa. Full notes are available on [GitHub Releases](https://github.com/kensa-sh/kensa/releases).

"""


def test_extract_release_notes_returns_git_cliff_section() -> None:
    changelog = f"""{CHANGELOG_PREAMBLE}## 1.2.3
### Features
* feat: add release previews ([abc1234](https://example.com/abc1234))

**Full Changelog**: https://example.com/compare/v1.2.2...v1.2.3

## 1.2.2
### Bug Fixes
* fix: previous release
"""

    assert (
        extract_release_notes(changelog, "1.2.3")
        == """## What's Changed
### Features
* feat: add release previews ([abc1234](https://example.com/abc1234))

**Full Changelog**: https://example.com/compare/v1.2.2...v1.2.3
"""
    )


def test_extract_release_notes_rejects_missing_version() -> None:
    with pytest.raises(ValueError, match=r"release 1\.2\.3 is missing"):
        extract_release_notes(f"{CHANGELOG_PREAMBLE}## 1.2.2\n", "1.2.3")


def test_extract_release_notes_rejects_unexpected_preamble() -> None:
    with pytest.raises(ValueError, match="unexpected preamble"):
        extract_release_notes("# Handwritten changelog\n\n## 1.2.3\n", "1.2.3")


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
