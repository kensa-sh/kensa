from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
RELEASE_SCRIPT = ROOT / "scripts" / "release.sh"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = ROOT / "pyproject.toml"


def test_release_pr_body_includes_complete_changelog_and_manual_merge() -> None:
    env = os.environ.copy()
    env["RELEASE_SCRIPT"] = str(RELEASE_SCRIPT)
    env["RELEASE_NOTES"] = """## What's Changed
### Features
* feat: include the complete changelog
"""

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$RELEASE_SCRIPT"; release_pr_body 1.2.3 "$RELEASE_NOTES" "$BASE_SHA"',
        ],
        check=True,
        capture_output=True,
        env={**env, "BASE_SHA": "a" * 40},
        text=True,
    )

    assert "## What's Changed" in result.stdout
    assert "* feat: include the complete changelog" in result.stdout
    assert f"<!-- release-notes-base: {'a' * 40} -->" in result.stdout
    assert "Review generated `CHANGELOG.md` for completeness." in result.stdout
    assert "Write user-facing v1.2.3 notes in `docs/changelog.mdx`." in result.stdout
    assert "scripts/changelog.py sync" not in result.stdout
    assert "Merge this PR manually after CI passes." in result.stdout
    assert "Merging publishes `kensa==1.2.3` to PyPI" in result.stdout


def test_assert_release_notes_base_accepts_matching_generation_sha() -> None:
    base_sha = "a" * 40
    env = os.environ.copy()
    env["BASE_SHA"] = base_sha
    env["PR_BODY"] = f"Release PR.\n\n<!-- release-notes-base: {base_sha} -->\n"
    env["RELEASE_SCRIPT"] = str(RELEASE_SCRIPT)

    subprocess.run(
        [
            "bash",
            "-c",
            'source "$RELEASE_SCRIPT"; assert_release_notes_base "$PR_BODY" "$BASE_SHA"',
        ],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )


def test_assert_release_notes_base_rejects_stale_generation_sha() -> None:
    env = os.environ.copy()
    env["BASE_SHA"] = "b" * 40
    env["PR_BODY"] = f"Release PR.\n\n<!-- release-notes-base: {'a' * 40} -->\n"
    env["RELEASE_SCRIPT"] = str(RELEASE_SCRIPT)

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$RELEASE_SCRIPT"; assert_release_notes_base "$PR_BODY" "$BASE_SHA"',
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    assert "release notes must be regenerated from the current PR base" in result.stderr


def test_generate_changelog_runs_git_cliff_for_pending_tag(tmp_path: Path) -> None:
    uv = tmp_path / "uv"
    uv.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$UV_ARGS_FILE"\n')
    uv.chmod(0o755)
    args_file = tmp_path / "args"
    env = os.environ.copy()
    env["UV_ARGS_FILE"] = str(args_file)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["RELEASE_SCRIPT"] = str(RELEASE_SCRIPT)

    subprocess.run(
        [
            "bash",
            "-c",
            'source "$RELEASE_SCRIPT"; generate_changelog 1.2.3',
        ],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    assert args_file.read_text().splitlines() == [
        "run",
        "git-cliff",
        "--config",
        str(ROOT / "cliff.toml"),
        "--repository",
        str(ROOT),
        "--tag",
        "v1.2.3",
        "--output",
        str(ROOT / "CHANGELOG.md"),
    ]


def test_release_uses_pinned_git_cliff_without_github_generation() -> None:
    script = RELEASE_SCRIPT.read_text()
    pyproject = PYPROJECT.read_text()

    assert 'generate_changelog "$version"' in script
    assert "generate_release_notes" not in script
    assert "rebuild_changelog" not in script
    assert '"git-cliff==2.13.1"' in pyproject


def test_release_script_exposes_no_manual_publish_action() -> None:
    result = subprocess.run(
        ["bash", str(RELEASE_SCRIPT), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "./scripts/release.sh publish" not in result.stdout
    assert "Merge the PR manually after CI passes." in result.stdout


def test_release_workflow_publishes_reviewed_changelog_notes() -> None:
    workflow = RELEASE_WORKFLOW.read_text()

    assert 'scripts/changelog.py notes "$VERSION"' in workflow
    assert 'scripts/changelog.py check-docs "$version"' in workflow
    assert "--notes-file -" in workflow
    assert "--generate-notes" not in workflow
    assert "scripts/changelog.py sync" not in workflow
    assert 'assert_release_notes_base "$PR_BODY" "$PR_BASE_SHA"' in workflow


def test_required_lint_check_rejects_stale_release_notes() -> None:
    workflow = CI_WORKFLOW.read_text()

    assert "Verify release notes match pull request base" in workflow
    assert "github.event.pull_request.base.sha" in workflow
    assert 'scripts/changelog.py check-docs "$version"' in workflow
    assert "scripts/changelog.py sync" not in workflow
    assert 'assert_release_notes_base "$PR_BODY" "$PR_BASE_SHA"' in workflow
    assert "Verify generated release changelog" in workflow
    assert "uv run git-cliff" in workflow
    assert "--config cliff.toml" in workflow
    assert '--tag "v$version"' in workflow
    assert "diff --unified CHANGELOG.md /tmp/CHANGELOG.md" in workflow
