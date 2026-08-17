from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
RELEASE_SCRIPT = ROOT / "scripts" / "release.sh"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


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


def test_generate_release_notes_uses_explicit_tag_range(
    tmp_path: Path,
) -> None:
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$@" > "$GH_ARGS_FILE"\n'
        "printf '%s\\n' 'generated notes'\n"
    )
    gh.chmod(0o755)
    args_file = tmp_path / "args"
    env = os.environ.copy()
    env["GH_ARGS_FILE"] = str(args_file)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["RELEASE_SCRIPT"] = str(RELEASE_SCRIPT)

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$RELEASE_SCRIPT"; generate_release_notes v1.2.3 v1.2.2 abc123',
        ],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.stdout == "generated notes\n"
    assert args_file.read_text().splitlines() == [
        "api",
        "--method",
        "POST",
        "repos/{owner}/{repo}/releases/generate-notes",
        "--raw-field",
        "tag_name=v1.2.3",
        "--raw-field",
        "previous_tag_name=v1.2.2",
        "--raw-field",
        "target_commitish=abc123",
        "--jq",
        ".body",
    ]


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
