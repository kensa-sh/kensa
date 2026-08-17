from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG_PATH = ROOT / "CHANGELOG.md"
DOCS_CHANGELOG_PATH = ROOT / "docs" / "changelog.mdx"

CHANGELOG_PREAMBLE = """# Changelog

Release notes for Kensa. Full notes are available on [GitHub Releases](https://github.com/kensa-sh/kensa/releases).

"""

DOCS_PREAMBLE = """---
title: Changelog
description: What's new in Kensa.
sidebarTitle: Changelog
keywords:
  - changelog
  - releases
  - version history
  - release notes
---

<Note>Release notes for Kensa. Full notes are available on [GitHub Releases](https://github.com/kensa-sh/kensa/releases).</Note>

"""

VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _validate_version(version: str) -> None:
    if VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError(f"invalid release version: {version}")


def _release_history(changelog: str) -> str:
    if not changelog.startswith(CHANGELOG_PREAMBLE):
        raise ValueError("CHANGELOG.md has an unexpected preamble")
    return changelog.removeprefix(CHANGELOG_PREAMBLE)


def build_release_section(version: str, generated_notes: str) -> str:
    _validate_version(version)
    lines = generated_notes.strip().splitlines()
    if lines and lines[0].startswith("<!-- Release notes generated"):
        if not lines[0].endswith("-->"):
            raise ValueError("generated release-notes comment is malformed")
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    if not lines:
        raise ValueError("generated release notes are empty")
    if lines[0] == "## What's Changed":
        lines[0] = f"## {version}"
    else:
        lines = [f"## {version}", "", *lines]
    section = "\n".join(lines).rstrip()
    return f"{section}\n"


def prepend_release(changelog: str, version: str, generated_notes: str) -> str:
    history = _release_history(changelog).rstrip()
    if f"## {version}" in history.splitlines():
        raise ValueError(f"release {version} already exists in CHANGELOG.md")
    section = build_release_section(version, generated_notes)
    return f"{CHANGELOG_PREAMBLE}{section}\n{history}\n"


def extract_release_notes(changelog: str, version: str) -> str:
    _validate_version(version)
    lines = _release_history(changelog).splitlines()
    heading = f"## {version}"
    try:
        start = lines.index(heading)
    except ValueError as error:
        raise ValueError(f"release {version} is missing from CHANGELOG.md") from error
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    release_lines = lines[start:end]
    release_lines[0] = "## What's Changed"
    release_notes = "\n".join(release_lines).rstrip()
    return f"{release_notes}\n"


def render_docs_changelog(changelog: str) -> str:
    history = _release_history(changelog).rstrip()
    return f"{DOCS_PREAMBLE}{history}\n"


def _sync_changelogs(changelog: str) -> None:
    CHANGELOG_PATH.write_text(changelog)
    DOCS_CHANGELOG_PATH.write_text(render_docs_changelog(changelog))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Kensa changelog files.")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="prepend generated notes for a release")
    add.add_argument("version")

    sync = commands.add_parser("sync", help="generate docs/changelog.mdx from CHANGELOG.md")
    sync.add_argument("--check", action="store_true")

    notes = commands.add_parser("notes", help="print release notes for a version")
    notes.add_argument("version")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        changelog = CHANGELOG_PATH.read_text()
        if args.command == "add":
            _sync_changelogs(prepend_release(changelog, args.version, sys.stdin.read()))
        elif args.command == "sync":
            rendered = render_docs_changelog(changelog)
            if args.check and DOCS_CHANGELOG_PATH.read_text() != rendered:
                message = "error: docs/changelog.mdx is not synchronized with CHANGELOG.md"
                print(message, file=sys.stderr)
                return 1
            if not args.check:
                DOCS_CHANGELOG_PATH.write_text(rendered)
        else:
            print(extract_release_notes(changelog, args.version), end="")
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
