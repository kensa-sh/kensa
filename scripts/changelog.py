from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG_PATH = ROOT / "CHANGELOG.md"
DOCS_CHANGELOG_PATH = ROOT / "docs" / "changelog.mdx"

CHANGELOG_PREAMBLE = """# Changelog

<!-- Generated from Git history by git-cliff. Do not edit manually. -->

Release notes for Kensa. Full notes are available on [GitHub Releases](https://github.com/kensa-sh/kensa/releases).

"""

VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
RELEASE_HEADING_PATTERN = re.compile(r"^## (0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _validate_version(version: str) -> None:
    if VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError(f"invalid release version: {version}")


def _release_history(changelog: str) -> str:
    if not changelog.startswith(CHANGELOG_PREAMBLE):
        raise ValueError("CHANGELOG.md has an unexpected preamble")
    return changelog.removeprefix(CHANGELOG_PREAMBLE)


def extract_release_notes(changelog: str, version: str) -> str:
    _validate_version(version)
    lines = _release_history(changelog).splitlines()
    heading = f"## {version}"
    try:
        start = lines.index(heading)
    except ValueError as error:
        raise ValueError(f"release {version} is missing from CHANGELOG.md") from error
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if RELEASE_HEADING_PATTERN.fullmatch(lines[index]) is not None
        ),
        len(lines),
    )
    release_lines = lines[start:end]
    release_lines[0] = "## What's Changed"
    release_notes = "\n".join(release_lines).rstrip()
    return f"{release_notes}\n"


def check_docs_release(version: str, docs_changelog: str) -> None:
    _validate_version(version)
    if f"## {version}" not in docs_changelog.splitlines():
        raise ValueError(f"docs/changelog.mdx is missing release {version}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Kensa changelog files.")
    commands = parser.add_subparsers(dest="command", required=True)

    check_docs = commands.add_parser(
        "check-docs", help="verify product release notes exist for a version"
    )
    check_docs.add_argument("version")

    notes = commands.add_parser("notes", help="print release notes for a version")
    notes.add_argument("version")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "check-docs":
            check_docs_release(args.version, DOCS_CHANGELOG_PATH.read_text())
            return 0
        changelog = CHANGELOG_PATH.read_text()
        print(extract_release_notes(changelog, args.version), end="")
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
