from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
RELEASE_SCRIPT = ROOT / "scripts" / "release.sh"


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
