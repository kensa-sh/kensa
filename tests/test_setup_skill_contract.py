from __future__ import annotations

from pathlib import Path

from kensa import cli

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT / "src" / "kensa" / "skill_templates"


def _skill_text(name: str) -> str:
    return (SKILL_ROOT / name / "SKILL.md").read_text()


def _skill_contract(name: str) -> str:
    return " ".join(_skill_text(name).split())


def _assert_in_order(text: str, fragments: tuple[str, ...]) -> None:
    positions = [text.index(fragment) for fragment in fragments]
    assert positions == sorted(positions)


def test_setup_skill_requires_fixed_source_backed_discovery() -> None:
    skill = _skill_contract("kensa-setup")

    _assert_in_order(
        skill,
        (
            "documented run paths",
            "application entrypoints",
            "tests and factories",
            "agent constructors or orchestrators",
            "model and tool call sites",
            "their callers",
        ),
    )
    assert "Inspect the repository read-only" in skill
    assert "Trace inward from a real application entrypoint" in skill
    assert "outward from model and tool call sites" in skill
    for evidence in (
        "exact source locations",
        "construction path",
        "input and output mapping",
        "conversation-state owner",
        "resource lifecycle",
        "external effects",
        "unresolved gaps",
    ):
        assert evidence in skill


def test_setup_skill_requires_proposal_and_approval_before_writes() -> None:
    skill = _skill_contract("kensa-setup")

    assert "Before editing, present" in skill
    assert "Wait for explicit user approval" in skill
    assert "real-model cost or live effects" in skill
    assert "Do not create or edit the fixture before approval" in skill
    assert "inspect and verify it, never silently overwrite it" in skill
    for proposal_field in (
        "production symbol and source location",
        "construction and invocation path",
        "input and output mapping",
        "conversation state and resource lifecycle",
        "external effects",
        "minimal fixture adapter",
        "unresolved gaps",
    ):
        assert proposal_field in skill


def test_setup_skill_preserves_the_production_ownership_boundary() -> None:
    skill = _skill_contract("kensa-setup")

    assert "The target repository owns" in skill
    assert "Kensa owns execution after fixture resolution" in skill
    assert "one production-owned conversation instance per trial" in skill
    assert "must not reproduce prompts, tools, routing, state, configuration, or lifecycle" in skill
    assert "thin repository-owned `kensa_run` adapter" in skill


def test_setup_skill_fails_closed_with_actionable_cannot_wire() -> None:
    skill = _skill_contract("kensa-setup")

    for blocker in (
        "asking the user to select among plausible production boundaries",
        "guessing an unresolved seam",
        "reproducing agent behavior",
        "bypassing production construction",
        "changing production code",
        "hiding an unsafe effect",
    ):
        assert blocker in skill
    assert "`cannot wire`" in skill
    assert "exact reason" in skill
    assert "target-owned decision or seam required" in skill
    assert "no fixture edit" in skill
    assert "no readiness claim" in skill


def test_evals_skill_stops_after_setup_cannot_wire() -> None:
    skill = _skill_contract("kensa-evals")

    assert "fixed, source-backed discovery sequence" in skill
    assert "explicit approval before editing" in skill
    assert "thin adapter around the approved production seam" in skill
    assert "If setup reports `cannot wire`" in skill
    assert "end the lifecycle" in skill
    assert "without importing evidence or claiming readiness" in skill


def test_setup_workflow_keeps_the_existing_fixture_contract() -> None:
    setup = _skill_contract("kensa-setup")
    evals = _skill_contract("kensa-evals")
    combined = f"{setup} {evals}"

    assert "tests/evals/conftest.py::kensa_run(case)" in combined
    assert "ConversationResponse" in combined
    assert "target_command" not in combined
    assert "JSON Lines" not in combined
    assert ".kensa/setup" not in combined


def test_init_installs_setup_workflow_verbatim(tmp_path: Path) -> None:
    target = tmp_path / ".agents" / "skills" / "kensa-evals" / "SKILL.md"

    cli._copy_skill_template_tree(target)

    for name in ("kensa-evals", "kensa-setup"):
        installed = tmp_path / ".agents" / "skills" / name / "SKILL.md"
        assert installed.read_text() == _skill_text(name)
