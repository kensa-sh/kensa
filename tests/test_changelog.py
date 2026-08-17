from __future__ import annotations

from pathlib import Path

import pytest

from scripts.changelog import (
    build_release_section,
    extract_release_notes,
    prepend_release,
    render_docs_changelog,
)

ROOT = Path(__file__).parents[1]

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


def test_render_docs_changelog_uses_canonical_release_history() -> None:
    history = "## 1.2.3\n\nRelease notes.\n"

    rendered = render_docs_changelog(f"{CHANGELOG_PREAMBLE}{history}")

    assert rendered.startswith("---\ntitle: Changelog\n")
    assert "<Note>Release notes for Kensa." in rendered
    assert rendered.endswith(history)


def test_committed_docs_changelog_matches_canonical_changelog() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text()
    docs_changelog = (ROOT / "docs/changelog.mdx").read_text()

    assert docs_changelog == render_docs_changelog(changelog)
