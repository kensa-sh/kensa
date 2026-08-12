from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[3]
PYTHON_PROJECT = ROOT / "sdk" / "python" / "pyproject.toml"
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
    assert "Merging publishes `kensa==1.2.3`" in result.stdout
    assert "`@kensa/core@1.2.3`" in result.stdout
    assert "`@kensa/sdk@1.2.3`" in result.stdout


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


def test_ci_reports_stable_build_status_after_build_jobs() -> None:
    workflow = CI_WORKFLOW.read_text()
    jobs_after_build = workflow.split("\n  build:\n", maxsplit=1)[1]
    build_job = jobs_after_build.split("\n  redaction:\n", maxsplit=1)[0]

    assert "if: ${{ always() }}" in build_job
    assert "needs: [typescript, wheels]" in build_job
    assert "TYPESCRIPT_RESULT: ${{ needs.typescript.result }}" in build_job
    assert "WHEELS_RESULT: ${{ needs.wheels.result }}" in build_job
    assert 'test "$TYPESCRIPT_RESULT" = "success"' in build_job
    assert 'test "$WHEELS_RESULT" = "success"' in build_job


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
    assert "\n  package-npm:\n" in workflow
    assert "\n  publish-npm-core:\n" in workflow
    assert "\n  publish-npm-sdk:\n" in workflow
    assert "\n  github-release:\n" in workflow
    assert "needs: [prepare, tag]" in workflow
    assert "needs: [prepare, publish-pypi, publish-npm-core, publish-npm-sdk]" in workflow
    assert workflow.index("\n  package-npm:\n") < workflow.index("\n  tag:\n")
    assert workflow.index("\n  tag:\n") < workflow.index("\n  publish-pypi:\n")
    assert workflow.index("\n  tag:\n") < workflow.index("\n  publish-npm-core:\n")
    assert workflow.index("\n  publish-npm-core:\n") < workflow.index("\n  publish-npm-sdk:\n")
    assert workflow.index("\n  publish-pypi:\n") < workflow.index("\n  github-release:\n")
    assert workflow.index("\n  publish-npm-sdk:\n") < workflow.index("\n  github-release:\n")


def test_release_workflow_requires_live_redaction_before_tagging() -> None:
    workflow = RELEASE_WORKFLOW.read_text()
    redaction_job = workflow.split("\n  redaction:\n", maxsplit=1)[1].split(
        "\n  build-wheels:\n", maxsplit=1
    )[0]

    assert "needs: prepare" in redaction_job
    assert "ref: ${{ needs.prepare.outputs.sha }}" in redaction_job
    assert "uv sync --group dev --all-packages --extra redaction" in redaction_job
    assert (
        "uv run pytest sdk/python/tests/integration/test_live_redaction.py -q --run-live"
        in redaction_job
    )
    assert "needs: [prepare, test, lint, redaction, build-wheels, package-npm]" in workflow


def test_release_workflow_publishes_only_verified_platform_wheels() -> None:
    workflow = RELEASE_WORKFLOW.read_text()
    publish_job = workflow.split("\n  publish-pypi:\n", maxsplit=1)[1].split(
        "\n  publish-npm-core:\n", maxsplit=1
    )[0]

    assert "dist-wheel-${{ matrix.target }}" in workflow
    assert "scripts/verify-engine-wheel.py dist" in workflow
    assert "auditwheel" not in workflow
    assert "pattern: dist-wheel-*" in publish_job
    assert "merge-multiple: true" in publish_job
    assert "skip-existing: true" in publish_job
    assert "--sdist" not in workflow


def test_wheel_workflows_install_the_baseline_windows_runtime() -> None:
    for workflow_path in (CI_WORKFLOW, RELEASE_WORKFLOW):
        workflow = workflow_path.read_text()

        assert "version=\"$(tr -d '\\r\\n' < .bun-version)\"" in workflow
        assert 'bun-v$version/bun-windows-x64-baseline.zip" >> "$GITHUB_OUTPUT"' in workflow
        assert "if: matrix.target != 'win32-x64'" in workflow
        assert "bun-download-url: ${{ steps.windows_bun.outputs.url }}" in workflow


def test_release_workflow_uses_npm_trusted_publishing() -> None:
    workflow = RELEASE_WORKFLOW.read_text()
    package_job = workflow.split("\n  package-npm:\n", maxsplit=1)[1].split(
        "\n  tag:\n", maxsplit=1
    )[0]
    core_job = workflow.split("\n  publish-npm-core:\n", maxsplit=1)[1].split(
        "\n  publish-npm-sdk:\n", maxsplit=1
    )[0]
    sdk_job = workflow.split("\n  publish-npm-sdk:\n", maxsplit=1)[1].split(
        "\n  github-release:\n", maxsplit=1
    )[0]

    assert "set-typescript-version.mjs --check" in package_job
    assert "pnpm --filter @kensa/core pack" in package_job
    assert "pnpm --filter @kensa/sdk pack" in package_job
    assert "name: dist-npm" in package_job
    assert "dist/npm/kensa-build-manifest.json" in package_job
    assert "needs: [prepare, tag, package-npm]" in core_job
    assert "needs: [prepare, tag, package-npm, publish-npm-core]" in sdk_job
    for job in (core_job, sdk_job):
        assert "id-token: write" in job
        assert "node-version: 24" in job
        assert "npm install --global npm@11.5.1" in job
        assert "name: dist-npm" in job
        assert "NODE_AUTH_TOKEN" not in job
    assert 'npm publish "dist/npm/kensa-core-$VERSION.tgz"' in core_job
    assert 'npm publish "dist/npm/kensa-sdk-$VERSION.tgz"' in sdk_job
    assert 'gh release create "$TAG" dist/*.whl dist/npm/kensa-build-manifest.json' in workflow


def test_typescript_package_versions_match_python_release() -> None:
    version = tomllib.loads(PYTHON_PROJECT.read_text())["project"]["version"]

    result = subprocess.run(
        ["node", "scripts/set-typescript-version.mjs", "--check", version],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    mismatch = subprocess.run(
        ["node", "scripts/set-typescript-version.mjs", "--check", "9.9.9"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert mismatch.returncode == 1
    assert "expected 9.9.9" in mismatch.stderr


def test_python_sdk_is_the_only_python_workspace_member() -> None:
    workspace = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = tomllib.loads(PYTHON_PROJECT.read_text())

    assert "project" not in workspace
    assert workspace["tool"]["uv"]["workspace"]["members"] == ["sdk/python"]
    assert project["project"]["name"] == "kensa"
    assert (ROOT / "sdk" / "python" / "src" / "kensa").is_dir()
    assert (ROOT / "sdk" / "python" / "tests").is_dir()
    assert not list((ROOT / "src").glob("**/*.py"))
    assert not list((ROOT / "tests").glob("**/*.py"))
