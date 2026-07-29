from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
RELEASE_SCRIPT = ROOT / "scripts" / "release.sh"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def test_release_pr_body_requires_changelog_update_and_manual_merge() -> None:
    env = os.environ.copy()
    env["RELEASE_SCRIPT"] = str(RELEASE_SCRIPT)

    result = subprocess.run(
        ["bash", "-c", 'source "$RELEASE_SCRIPT"; release_pr_body 1.2.3'],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    assert "- [ ] Update `docs/changelog.mdx` for v1.2.3." in result.stdout
    assert "Merge this PR manually after CI passes." in result.stdout
    assert "Merging publishes `kensa==1.2.3` to PyPI" in result.stdout


def test_release_script_exposes_no_manual_publish_action() -> None:
    result = subprocess.run(
        ["bash", str(RELEASE_SCRIPT), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "./scripts/release.sh publish" not in result.stdout
    assert "Merge the PR manually after CI passes." in result.stdout


def test_ci_does_not_merge_release_pull_requests() -> None:
    workflow = CI_WORKFLOW.read_text()

    assert "merge-release:" not in workflow
    assert "repos/${GH_REPO}/pulls/${PR_NUMBER}/merge" not in workflow


def test_release_workflow_requires_a_merged_release_pull_request() -> None:
    workflow = RELEASE_WORKFLOW.read_text()

    assert "types: [closed]" in workflow
    assert "github.event.pull_request.merged == true" in workflow
    assert "github.event.pull_request.user.login == 'satyaborg'" in workflow
    assert "contains(github.event.pull_request.labels.*.name, 'ignore-for-release')" in workflow
    assert "ref: ${{ github.event.pull_request.merge_commit_sha }}" in workflow
    assert 'git merge-base --is-ancestor "$MERGE_SHA" refs/remotes/origin/main' in workflow


def test_release_workflow_separates_tag_and_publication_jobs() -> None:
    workflow = RELEASE_WORKFLOW.read_text()

    assert "\n  tag:\n" in workflow
    assert "\n  publish-pypi:\n" in workflow
    assert "\n  github-release:\n" in workflow
    assert "needs: [prepare, tag]" in workflow
    assert "needs: [prepare, publish-pypi]" in workflow
    assert workflow.index("\n  tag:\n") < workflow.index("\n  publish-pypi:\n")
    assert workflow.index("\n  publish-pypi:\n") < workflow.index("\n  github-release:\n")


def test_release_workflow_requires_live_redaction_before_tagging() -> None:
    workflow = RELEASE_WORKFLOW.read_text()
    redaction_job = workflow.split("\n  redaction:\n", maxsplit=1)[1].split(
        "\n  build:\n", maxsplit=1
    )[0]

    assert "needs: prepare" in redaction_job
    assert "ref: ${{ needs.prepare.outputs.sha }}" in redaction_job
    assert 'uv pip install ".[redaction]"' in redaction_job
    assert "uv run pytest tests/integration/test_live_redaction.py -q --run-live" in redaction_job
    assert "needs: [prepare, test, lint, redaction, build]" in workflow
