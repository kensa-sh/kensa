from __future__ import annotations

import json
import random
import runpy
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT / "src" / "kensa" / "skill_templates" / "kensa-setup"
DETECTOR_PATH = SKILL_ROOT / "scripts" / "detect_frameworks.py"
REGISTRY_PATH = SKILL_ROOT / "assets" / "frameworks.json"

ENTRY_KEYS = frozenset(
    {
        "id",
        "kind",
        "name",
        "distributions",
        "import_roots",
        "aliases",
        "replaced_by",
        "documentation",
        "repository",
    }
)
FRAMEWORK_IDS = frozenset(
    {
        "ag2",
        "agency-swarm",
        "agent-s",
        "agentscope",
        "agno",
        "atomic-agents",
        "autogen",
        "beeai-framework",
        "browser-use",
        "camel",
        "chatdev",
        "claude-agent-sdk",
        "crewai",
        "deep-agents",
        "dspy",
        "google-adk",
        "haystack",
        "langchain",
        "langgraph",
        "langroid",
        "letta",
        "livekit-agents",
        "llamaindex",
        "marvin",
        "mcp-agent",
        "metagpt",
        "microsoft-agent-framework",
        "mirascope",
        "nvidia-nemo-agent-toolkit",
        "openai-agents-sdk",
        "openai-swarm",
        "parlant",
        "pipecat",
        "pydantic-ai",
        "rasa",
        "semantic-kernel",
        "smolagents",
        "strands-agents",
        "swarms",
        "taskweaver",
    }
)
CLIENT_IDS = frozenset(
    {
        "amazon-bedrock",
        "anthropic",
        "cohere",
        "google-genai",
        "litellm",
        "mistral",
        "openai",
    }
)
RECIPE_MARKERS = ("(", ")", "import ", "class ", "def ", "=", "await ", "yield ")
_DESCRIPTIVE_SECTIONS = frozenset({"notes", "scan_policy"})
FORBIDDEN_OUTPUT_KEY_MARKERS = (
    "confidence",
    "production",
    "ready",
    "recommend",
    "safe",
    "score",
    "target",
)


def _load_detector() -> Any:
    """Load the bundled detector without writing bytecode into the template tree."""
    module = ModuleType("kensa_detect_frameworks")
    module.__file__ = str(DETECTOR_PATH)
    source = DETECTOR_PATH.read_text(encoding="utf-8")
    exec(compile(source, str(DETECTOR_PATH), "exec"), module.__dict__)
    return module


detector: Any = _load_detector()
REGISTRY: dict[str, Any] = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
ENTRIES: list[dict[str, Any]] = REGISTRY["entries"]


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _scan(root: Path) -> dict[str, Any]:
    return detector.scan_repository(root.resolve())


def _detected(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {record["id"]: record for record in document["detected"]}


def _walk_values(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return [value for item in node.values() for value in _walk_values(item)]
    if isinstance(node, list):
        return [value for item in node for value in _walk_values(item)]
    return [node]


def _walk_keys(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [key for name, item in node.items() for key in [name, *_walk_keys(item)]]
    if isinstance(node, list):
        return [key for item in node for key in _walk_keys(item)]
    return []


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


# --- Registry identity ------------------------------------------------------


@pytest.mark.parametrize("entry", ENTRIES, ids=[entry["id"] for entry in ENTRIES])
def test_registry_entry_declares_minimal_stable_identity(entry: dict[str, Any]) -> None:
    assert set(entry) == ENTRY_KEYS
    assert entry["kind"] in {"framework", "client"}
    assert entry["id"] == entry["id"].strip().lower()
    assert entry["name"].strip()
    assert isinstance(entry["distributions"], list)
    assert entry["import_roots"], "every entry needs at least one import root"
    assert isinstance(entry["aliases"], list)
    for distribution in [*entry["distributions"], *entry["aliases"]]:
        assert distribution == distribution.strip().lower()
    assert not set(entry["distributions"]) & set(entry["aliases"])
    for import_root in entry["import_roots"]:
        assert all(part.isidentifier() for part in import_root.split("."))
    assert entry["documentation"].startswith("https://")
    assert entry["repository"].startswith("https://")
    assert entry["distributions"] or entry["repository"]


@pytest.mark.parametrize("entry", ENTRIES, ids=[entry["id"] for entry in ENTRIES])
def test_registry_entry_carries_no_api_recipe_or_invocation_symbol(entry: dict[str, Any]) -> None:
    identity_only = {
        key: value
        for key, value in entry.items()
        if key not in {"documentation", "repository", "name"}
    }
    for value in _walk_values(identity_only):
        if not isinstance(value, str):
            continue
        for marker in RECIPE_MARKERS:
            assert marker not in value, f"{entry['id']} carries invocation detail: {value}"


def test_registry_ids_are_unique_and_cover_every_named_framework_and_client() -> None:
    ids = [entry["id"] for entry in ENTRIES]
    assert len(ids) == len(set(ids))
    assert {entry["id"] for entry in ENTRIES if entry["kind"] == "framework"} == FRAMEWORK_IDS
    assert {entry["id"] for entry in ENTRIES if entry["kind"] == "client"} == CLIENT_IDS


def test_registry_names_the_maintained_replacement_for_superseded_frameworks() -> None:
    replacements = {
        entry["id"]: entry["replaced_by"] for entry in ENTRIES if entry["replaced_by"] is not None
    }
    assert replacements == {
        "autogen": "microsoft-agent-framework",
        "semantic-kernel": "microsoft-agent-framework",
        "openai-swarm": "openai-agents-sdk",
    }
    known = {entry["id"] for entry in ENTRIES}
    for entry_id, replacement in replacements.items():
        assert replacement in known
        assert replacement != entry_id


def test_registry_documents_its_identity_only_contract() -> None:
    assert REGISTRY["schema_version"] == "kensa.framework_registry.v1"
    assert isinstance(REGISTRY["revision"], int)
    assert "No constructors" in REGISTRY["contract"]


# --- Determinism ------------------------------------------------------------


def _determinism_fixture(root: Path) -> None:
    _write(
        root,
        "pyproject.toml",
        '[project]\nname = "demo"\ndependencies = ["langgraph>=0.2", "openai"]\n',
    )
    _write(root, "requirements-dev.txt", "crewai==1.0.0\n")
    _write(
        root,
        "src/app/agent.py",
        "from langgraph.graph import StateGraph\nimport openai\n\n"
        "graph = StateGraph(dict)\nclient = openai.OpenAI()\n",
    )
    _write(root, "src/app/__init__.py", "")
    _write(root, "src/app/helpers.py", "from crewai import Crew\n")


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_scan_is_byte_identical_across_parents_and_shuffled_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed: int,
) -> None:
    first = tmp_path / "checkout-a" / "repo"
    second = tmp_path / "b" / "deeper" / "checkout" / "repo"
    for root in (first, second):
        root.mkdir(parents=True)
        _determinism_fixture(root)

    baseline = json.dumps(_scan(first), indent=2)

    original_iterdir = Path.iterdir

    def shuffled_iterdir(self: Path) -> Any:
        entries = list(original_iterdir(self))
        random.Random(seed).shuffle(entries)
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", shuffled_iterdir)
    assert json.dumps(_scan(second), indent=2) == baseline
    assert json.dumps(_scan(first), indent=2) == baseline


# --- Evidence separation ----------------------------------------------------


def test_declared_dependency_without_import_stays_declared_only(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", '[project]\ndependencies = ["crewai>=1.0"]\n')
    _write(tmp_path, "app.py", "value = 1\n")

    document = _scan(tmp_path)

    record = _detected(document)["crewai"]
    assert record["state"] == "declared_only"
    assert record["declared"]
    assert not record["imported"]
    assert not record["call_referenced"]
    assert record["specifiers"] == [">=1.0"]
    assert record["versions"] == []
    assert document["imports"] == []
    assert document["call_references"] == []


def test_import_without_call_reference_stays_imported_only(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "from crewai import Crew\n\nCREWS: list[Crew] = []\n")

    document = _scan(tmp_path)

    record = _detected(document)["crewai"]
    assert record["state"] == "imported_only"
    assert record["imported"]
    assert not record["declared"]
    assert record["import_roots"] == ["crewai"]
    assert document["imports"][0]["symbol"] == "Crew"
    assert document["imports"][0]["path"] == "app.py"
    assert document["call_references"] == []


def test_aliased_import_and_attribute_call_are_reported_as_call_references(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app.py",
        "import langgraph.graph as lg\nfrom crewai import Crew as Squad\n\n"
        "builder = lg.StateGraph(dict)\nsquad = Squad()\n",
    )

    document = _scan(tmp_path)

    references = {(item["id"], item["reference"]) for item in document["call_references"]}
    assert references == {("langgraph", "lg.StateGraph"), ("crewai", "Squad")}
    assert _detected(document)["langgraph"]["state"] == "call_referenced"
    bindings = {item["binding"] for item in document["imports"]}
    assert bindings == {"lg", "Squad"}


@pytest.mark.parametrize(
    ("source", "entry_id", "expected_reference"),
    [
        ("from google import adk\n\nagent = adk.Agent()\n", "google-adk", "adk.Agent"),
        ("from google import genai\n\nclient = genai.Client()\n", "google-genai", "genai.Client"),
        (
            "from livekit import agents\n\nsession = agents.AgentSession()\n",
            "livekit-agents",
            "agents.AgentSession",
        ),
        (
            "from livekit import agents as lk\n\nsession = lk.AgentSession()\n",
            "livekit-agents",
            "lk.AgentSession",
        ),
        (
            "from google.adk.agents import Agent\n\nagent = Agent()\n",
            "google-adk",
            "Agent",
        ),
        ("import google.adk\n\nagent = google.adk.Agent()\n", "google-adk", "google.adk.Agent"),
        (
            "from google.adk import agents\n\nagent = agents.Agent()\n",
            "google-adk",
            "agents.Agent",
        ),
    ],
)
def test_dotted_registry_roots_match_every_absolute_import_spelling(
    tmp_path: Path,
    source: str,
    entry_id: str,
    expected_reference: str,
) -> None:
    _write(tmp_path, "app.py", source)

    document = _scan(tmp_path)

    assert _detected(document)[entry_id]["state"] == "call_referenced"
    assert [item["reference"] for item in document["call_references"]] == [expected_reference]


def test_namespace_parent_import_alone_is_not_a_framework_match(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "from google import protobuf\n\nvalue = protobuf.Message()\n")

    document = _scan(tmp_path)

    assert document["detected"] == []


def test_star_import_of_a_framework_module_is_recorded_without_a_call_reference(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app.py", "from google.adk import *\n")

    document = _scan(tmp_path)

    assert document["imports"][0]["symbol"] == "*"
    assert document["call_references"] == []
    assert _detected(document)["google-adk"]["state"] == "imported_only"


def test_multiple_frameworks_and_clients_under_one_repository_are_all_reported(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\ndependencies = ["ag2", "autogen-agentchat", "boto3"]\n',
    )
    _write(
        tmp_path,
        "app.py",
        "import autogen\nfrom anthropic import Anthropic\n\n"
        "team = autogen.GroupChat()\nclient = Anthropic()\n",
    )

    document = _scan(tmp_path)
    detected = _detected(document)

    assert {"ag2", "autogen"} <= set(detected)
    assert detected["ag2"]["state"] == "call_referenced"
    assert detected["autogen"]["state"] == "call_referenced"
    assert detected["anthropic"]["kind"] == "client"
    assert detected["amazon-bedrock"]["state"] == "declared_only"
    assert [record["kind"] for record in document["detected"]] == sorted(
        (record["kind"] for record in document["detected"]), key=lambda kind: kind != "framework"
    )


def test_alias_distribution_declaration_records_the_alias_match(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "requirements.txt",
        "PyAutoGen[retrievechat]>=0.2 ; python_version >= '3.10'\n",
    )

    document = _scan(tmp_path)

    declaration = document["declarations"][0]
    assert declaration["id"] == "autogen"
    assert declaration["distribution"] == "pyautogen"
    assert declaration["registry_match"] == "alias"
    assert declaration["specifier"] == ">=0.2"
    assert _detected(document)["autogen"]["replaced_by"] == "microsoft-agent-framework"


def test_local_name_collision_drops_call_references_and_reports_a_gap(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "from crewai import Crew\n\n\ndef Crew():\n    return None\n\n\nvalue = Crew()\n",
    )

    document = _scan(tmp_path)

    assert document["call_references"] == []
    assert document["imports"]
    assert _detected(document)["crewai"]["state"] == "imported_only"
    assert [gap["kind"] for gap in document["gaps"]] == ["shadowed_import_binding"]
    assert document["gaps"][0]["path"] == "app.py"


def test_first_party_module_shadowing_a_registry_import_root_is_reported_as_a_gap(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "agents/__init__.py", "")
    _write(
        tmp_path,
        "app.py",
        "from agents import Agent\nfrom agents import Runner\n\nagent = Agent()\n",
    )

    document = _scan(tmp_path)

    assert document["detected"] == []
    assert document["imports"] == []
    assert document["call_references"] == []
    assert document["gaps"] == [
        {
            "kind": "first_party_shadowed_import_root",
            "path": "app.py",
            "detail": (
                "Import agents resolves to a repository-local module of the same name; "
                "suppressed registry matches: openai-agents-sdk. Confirm this import by hand."
            ),
        }
    ]


def test_shadowed_import_gaps_name_every_suppressed_registry_entry(tmp_path: Path) -> None:
    _write(tmp_path, "autogen/__init__.py", "")
    _write(tmp_path, "app.py", "import autogen\n")

    gaps = _scan(tmp_path)["gaps"]

    assert len(gaps) == 1
    assert "suppressed registry matches: ag2, autogen." in str(gaps[0]["detail"])


def test_unknown_custom_agent_yields_no_framework_evidence(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", '[project]\ndependencies = ["acme-private-agents"]\n')
    _write(
        tmp_path,
        "app.py",
        "from acme.agents import Runtime\n\nruntime = Runtime()\n",
    )

    document = _scan(tmp_path)

    assert document["detected"] == []
    assert document["declarations"] == []
    assert document["gaps"] == []


def test_scan_output_makes_no_selection_readiness_or_confidence_claim(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "from crewai import Crew\n\ncrew = Crew()\n")

    document = _scan(tmp_path)

    evidence = {key: value for key, value in document.items() if key not in _DESCRIPTIVE_SECTIONS}
    for key in _walk_keys(evidence):
        for marker in FORBIDDEN_OUTPUT_KEY_MARKERS:
            assert marker not in key.lower(), f"output key {key} implies a judgement"
    assert "not evidence that the call runs" in document["notes"]["call_references"]
    assert "makes no readiness or safety claim" in document["notes"]["scope"]
    assert document["schema_version"] == "kensa.framework_scan.v1"
    assert document["registry_schema_version"] == REGISTRY["schema_version"]
    assert document["registry_revision"] == REGISTRY["revision"]


# --- Dependency manifests ---------------------------------------------------


def test_pyproject_declaration_groups_are_all_collected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pyproject.toml",
        """
[project]
dependencies = ["langgraph>=0.2", "-e ."]
optional-dependencies = { voice = ["pipecat-ai"], ui = ["browser-use"] }

[dependency-groups]
dev = ["smolagents", { include-group = "test" }]

[tool.poetry.dependencies]
python = "^3.11"
crewai = "^1.0"
dspy = { version = "^3.0" }

[tool.poetry.group.dev.dependencies]
marvin = "^3.0"

[tool.poetry.group.broken]
dependencies = 7

[tool.uv]
dev-dependencies = ["agno"]

[tool.pdm.dev-dependencies]
lint = ["mirascope"]
""",
    )

    document = _scan(tmp_path)

    assert set(_detected(document)) == {
        "agno",
        "browser-use",
        "crewai",
        "dspy",
        "langgraph",
        "marvin",
        "mirascope",
        "pipecat",
        "smolagents",
    }
    assert _detected(document)["dspy"]["specifiers"] == []
    assert _detected(document)["crewai"]["specifiers"] == ["^1.0"]


def test_pyproject_with_unexpected_shapes_is_tolerated(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pyproject.toml",
        """
project = "not-a-table"
dependency-groups = "not-a-table"
tool = "not-a-table"
""",
    )

    assert _scan(tmp_path)["detected"] == []


def test_pyproject_with_unexpected_tool_shapes_is_tolerated(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pyproject.toml",
        """
[project]
optional-dependencies = "not-a-table"

[tool]
poetry = "not-a-table"
uv = "not-a-table"

[tool.pdm]
dev-dependencies = "not-a-table"
""",
    )

    assert _scan(tmp_path)["detected"] == []


@pytest.mark.parametrize("lock_name", ["uv.lock", "poetry.lock", "pdm.lock"])
def test_toml_locks_supply_exact_version_evidence(tmp_path: Path, lock_name: str) -> None:
    _write(
        tmp_path,
        lock_name,
        """
[[package]]
name = "langgraph"
version = "0.2.14"

[[package]]
name = "pydantic-ai-slim"

[[package]]
name = 7

[[package]]
name = "letta"
version = 9

"not-a-package" = true
""",
    )

    detected = _detected(_scan(tmp_path))

    assert detected["langgraph"]["versions"] == ["0.2.14"]
    assert detected["pydantic-ai"]["versions"] == []
    assert detected["letta"]["versions"] == []


def test_toml_lock_with_unexpected_shapes_is_tolerated(tmp_path: Path) -> None:
    _write(tmp_path, "uv.lock", 'version = 1\npackage = "not-a-list"\n')
    _write(tmp_path, "pdm.lock", 'package = [1, { name = "letta", version = "0.16.8" }]\n')
    _write(tmp_path, "poetry.lock", "[package\n")

    document = _scan(tmp_path)

    assert _detected(document)["letta"]["versions"] == ["0.16.8"]
    assert [(gap["kind"], gap["path"]) for gap in document["gaps"]] == [
        ("manifest_parse_error", "poetry.lock")
    ]


def test_pipenv_lock_default_and_develop_sections_are_collected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Pipfile.lock",
        json.dumps(
            {
                "default": {"haystack-ai": {"version": "==3.0.0"}, "letta": "not-a-table"},
                "develop": {"marvin": {}},
                "_meta": "ignored",
            }
        ),
    )

    detected = _detected(_scan(tmp_path))

    assert detected["haystack"]["specifiers"] == ["==3.0.0"]
    assert detected["letta"]["specifiers"] == []
    assert detected["marvin"]["specifiers"] == []


def test_requirements_options_comments_and_direct_references_are_handled(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "requirements.txt",
        "\n".join(
            [
                "# comment only",
                "",
                "--index-url https://example.invalid/simple",
                "==1.0",
                "camel-ai==0.2.90  # pinned",
                "swarms @ git+https://github.com/kyegomez/swarms#egg=swarms",
            ]
        ),
    )

    document = _scan(tmp_path)
    detected = _detected(document)

    assert detected["camel"]["specifiers"] == ["==0.2.90"]
    assert detected["swarms"]["specifiers"] == []
    assert document["gaps"] == []


def test_unfollowed_requirement_includes_are_reported_as_gaps(tmp_path: Path) -> None:
    _write(tmp_path, "requirements-base.txt", "crewai==1.0.0\n")
    _write(
        tmp_path,
        "requirements.txt",
        "\n".join(
            [
                "-r requirements-base.txt",
                "-r base/common.txt",
                "--requirement=base/common.txt",
                "-c ../outside/constraints.txt",
                "--constraint",
                "-c",
            ]
        ),
    )

    document = _scan(tmp_path)

    assert _detected(document)["crewai"]["specifiers"] == ["==1.0.0"]
    assert [(gap["kind"], gap["detail"]) for gap in document["gaps"]] == [
        (
            "unsupported_requirement_include",
            "Includes --constraint an unnamed file, which this scan does not follow. "
            "Declarations in that file are missing from this evidence.",
        ),
        (
            "unsupported_requirement_include",
            "Includes --requirement base/common.txt, which this scan does not follow. "
            "Declarations in that file are missing from this evidence.",
        ),
        (
            "unsupported_requirement_include",
            "Includes -c a file outside the repository, which this scan does not follow. "
            "Declarations in that file are missing from this evidence.",
        ),
        (
            "unsupported_requirement_include",
            "Includes -c an unnamed file, which this scan does not follow. "
            "Declarations in that file are missing from this evidence.",
        ),
        (
            "unsupported_requirement_include",
            "Includes -r base/common.txt, which this scan does not follow. "
            "Declarations in that file are missing from this evidence.",
        ),
    ]
    assert all(gap["path"] == "requirements.txt" for gap in document["gaps"])


def test_malformed_manifests_are_reported_as_gaps(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project\n")
    _write(tmp_path, "Pipfile.lock", "{not json")

    gaps = _scan(tmp_path)["gaps"]

    assert {gap["path"] for gap in gaps} == {"pyproject.toml", "Pipfile.lock"}
    assert {gap["kind"] for gap in gaps} == {"manifest_parse_error"}


@pytest.mark.parametrize("payload", ["[]", '{"default": "not-a-table"}'])
def test_pipenv_lock_with_unexpected_shapes_is_tolerated(tmp_path: Path, payload: str) -> None:
    _write(tmp_path, "Pipfile.lock", payload)

    document = _scan(tmp_path)

    assert document["detected"] == []
    assert document["gaps"] == []


def test_oversized_manifests_are_skipped_with_a_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detector, "MAX_FILE_BYTES", 10)
    _write(tmp_path, "pyproject.toml", '[project]\ndependencies = ["crewai"]\n')

    document = _scan(tmp_path)

    assert document["detected"] == []
    assert [(gap["kind"], gap["path"]) for gap in document["gaps"]] == [
        ("file_too_large", "pyproject.toml")
    ]


# --- Source syntax ----------------------------------------------------------


def test_source_binding_forms_never_produce_false_call_references(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        """
from crewai import Crew
from . import sibling
from langgraph.graph import StateGraph

alpha = 1
beta: int = 2
beta += 1
(gamma := 3)
delta, [epsilon, *zeta] = (1, [2, 3])
for eta in range(1):
    pass
with open("x") as theta:
    pass
try:
    pass
except ValueError as iota:
    pass
kappa = [mu for mu in range(1)]
obj.attr = 4
lookup["key"] = 5


class Nu:
    pass


def xi(omicron):
    global alpha
    nonlocal_holder = 1

    def inner():
        nonlocal nonlocal_holder
        nonlocal_holder = 2

    return inner


match alpha:
    case [1, *rho]:
        pass
    case sigma:
        pass

crew = Crew()
graph = StateGraph(dict)
xi(1)()
lookup["key"]()
""",
    )

    document = _scan(tmp_path)

    references = {(item["id"], item["reference"]) for item in document["call_references"]}
    assert references == {("crewai", "Crew"), ("langgraph", "StateGraph")}
    assert document["gaps"] == []


def test_source_files_without_framework_imports_produce_no_evidence(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "import os\n\nvalue = os.getcwd()\n")

    document = _scan(tmp_path)

    assert document["detected"] == []
    assert document["gaps"] == []


def test_unparseable_source_is_reported_as_a_gap(tmp_path: Path) -> None:
    _write(tmp_path, "broken.py", "def (:\n")

    gaps = _scan(tmp_path)["gaps"]

    assert gaps[0]["kind"] == "source_parse_error"
    assert gaps[0]["path"] == "broken.py"
    assert "SyntaxError" in str(gaps[0]["detail"])


def test_first_party_roots_ignore_symlinks_and_plain_directories(tmp_path: Path) -> None:
    _write(tmp_path, "src/agents/__init__.py", "")
    _write(tmp_path, "notes/readme.md", "text")
    _write(tmp_path, "swarm.py", "value = 1\n")
    (tmp_path / "src" / "linked").symlink_to(tmp_path / "src" / "agents")
    _write(tmp_path, "app.py", "from agents import Agent\nfrom swarm import Swarm\n")

    document = _scan(tmp_path)

    assert document["detected"] == []
    assert {gap["kind"] for gap in document["gaps"]} == {"first_party_shadowed_import_root"}


# --- Safety -----------------------------------------------------------------


def _safety_fixture(root: Path) -> None:
    _write(root, ".env", "OPENAI_API_KEY=sk-live-do-not-read\n")
    _write(root, ".env.local", "ANTHROPIC_API_KEY=sk-live-do-not-read\n")
    _write(root, "secrets/credentials.json", '{"token": "sk-live-do-not-read"}')
    _write(root, "pyproject.toml", '[project]\ndependencies = ["langgraph"]\n')
    _write(
        root,
        "app.py",
        'from langgraph.graph import StateGraph\n\nTOKEN = "SUPER_SECRET_LITERAL_VALUE"\n'
        "graph = StateGraph(dict)\n",
    )


def test_detector_reads_only_dependency_manifests_and_python_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _safety_fixture(tmp_path)
    opened: list[str] = []
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes

    def recording_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(str(self))
        return original_read_text(self, *args, **kwargs)

    def recording_read_bytes(self: Path) -> bytes:
        opened.append(str(self))
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_text", recording_read_text)
    monkeypatch.setattr(Path, "read_bytes", recording_read_bytes)
    monkeypatch.setattr("builtins.open", _forbidden_open)

    document = _scan(tmp_path)

    inside = [path for path in opened if path.startswith(str(tmp_path))]
    assert sorted(inside) == sorted(
        [str(tmp_path / "pyproject.toml"), str(tmp_path / "app.py")],
    )
    assert opened.count(str(REGISTRY_PATH)) == 1
    assert _detected(document)["langgraph"]["state"] == "call_referenced"


def _forbidden_open(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(f"detector opened a file handle directly: {args!r}")


def test_detector_never_mutates_the_target_repository(tmp_path: Path) -> None:
    _safety_fixture(tmp_path)
    before = _tree_snapshot(tmp_path)

    _scan(tmp_path)

    assert _tree_snapshot(tmp_path) == before


def test_detector_source_declares_no_execution_network_or_write_capability() -> None:
    source = DETECTOR_PATH.read_text(encoding="utf-8")

    for banned in (
        "subprocess",
        "socket",
        "urllib",
        "requests",
        "httpx",
        "importlib",
        "__import__",
        "eval(",
        "exec(",
        "os.system",
        "write_text",
        "write_bytes",
        "shutil",
        "dotenv",
    ):
        assert banned not in source, f"detector references {banned}"


def test_output_carries_no_absolute_paths_or_raw_source_excerpts(tmp_path: Path) -> None:
    _safety_fixture(tmp_path)

    rendered = json.dumps(_scan(tmp_path), indent=2)

    assert str(tmp_path) not in rendered
    assert "SUPER_SECRET_LITERAL_VALUE" not in rendered
    assert "sk-live-do-not-read" not in rendered
    assert ".env" not in rendered
    for value in _walk_values(json.loads(rendered)):
        if isinstance(value, str):
            assert not value.startswith("/")


def test_symlinks_are_reported_and_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside, "leaked.py", "from crewai import Crew\n\ncrew = Crew()\n")
    repo = tmp_path / "repo"
    _write(repo, "app.py", "value = 1\n")
    _write(repo, "pkg/local.py", "value = 2\n")
    (repo / "escape").symlink_to(outside)
    (repo / "loop").symlink_to(repo / "pkg")
    (repo / "alias.py").symlink_to(repo / "pkg" / "local.py")

    document = _scan(repo)

    assert document["exclusions"] == [
        {"kind": "symlink_inside_repository", "path": "alias.py"},
        {"kind": "symlink_outside_repository", "path": "escape"},
        {"kind": "symlink_inside_repository", "path": "loop"},
    ]
    assert document["detected"] == []


def test_excluded_and_ignored_directories_are_never_scanned(tmp_path: Path) -> None:
    _write(tmp_path, ".venv/lib/site.py", "from crewai import Crew\n")
    _write(tmp_path, "build/generated.py", "from crewai import Crew\n")
    _write(tmp_path, "__pycache__/cached.py", "from crewai import Crew\n")
    _write(tmp_path, ".git/hook.py", "from crewai import Crew\n")
    _write(tmp_path, "app.py", "value = 1\n")

    document = _scan(tmp_path)

    assert document["detected"] == []
    assert document["exclusions"] == [
        {"kind": "excluded_directory", "path": ".venv"},
        {"kind": "excluded_directory", "path": "build"},
    ]
    assert document["scan_policy"]["executes_target_code"] is False
    assert document["scan_policy"]["network_access"] is False
    assert document["scan_policy"]["reads_credential_files"] is False


def test_scan_limits_appear_as_gaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detector, "MAX_SOURCE_FILES", 2)
    monkeypatch.setattr(detector, "MAX_FILE_BYTES", 40)
    for index in range(3):
        _write(tmp_path, f"module_{index}.py", "from crewai import Crew\n" * 5)

    gaps = _scan(tmp_path)["gaps"]

    assert [gap["kind"] for gap in gaps] == [
        "file_too_large",
        "file_too_large",
        "source_file_limit",
    ]
    assert [gap["path"] for gap in gaps] == ["module_0.py", "module_1.py", None]
    assert "first 2 of 3" in str(gaps[2]["detail"])


def test_unreadable_files_are_reported_as_gaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, "app.py", "value = 1\n")
    original_read_text = Path.read_text

    def failing_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == "app.py":
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "invalid start byte")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)

    gaps = _scan(tmp_path)["gaps"]

    assert gaps[0]["kind"] == "unreadable_file"
    assert gaps[0]["path"] == "app.py"


# --- Command line -----------------------------------------------------------


def test_cli_prints_a_json_document(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(tmp_path, "app.py", "from crewai import Crew\n\ncrew = Crew()\n")

    assert detector.main(["--root", str(tmp_path)]) == 0

    document = json.loads(capsys.readouterr().out)
    assert _detected(document)["crewai"]["state"] == "call_referenced"


def test_cli_rejects_a_missing_root(capsys: pytest.CaptureFixture[str]) -> None:
    assert detector.main(["--root", "does-not-exist"]) == 2
    assert "Not a directory" in capsys.readouterr().err


def test_cli_reports_an_unavailable_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(detector, "REGISTRY_PATH", tmp_path / "missing.json")

    assert detector.main(["--root", str(tmp_path)]) == 1
    assert "Framework registry is unavailable" in capsys.readouterr().err


def test_detector_module_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(tmp_path, "app.py", "from crewai import Crew\n\ncrew = Crew()\n")
    monkeypatch.setattr(sys, "argv", ["detect_frameworks", "--root", str(tmp_path)])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(DETECTOR_PATH), run_name="__main__")

    assert excinfo.value.code == 0
    assert '"crewai"' in capsys.readouterr().out


# --- Packaged skill contract ------------------------------------------------


def test_setup_skill_directly_references_the_detector_and_registry() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "scripts/detect_frameworks.py" in skill
    assert "assets/frameworks.json" in skill
    assert "skills/kensa-setup/scripts/detect_frameworks.py --root ." in skill
    assert "Replace `.claude/skills` with the skills root" in skill
    assert not sorted(SKILL_ROOT.glob("references/*")), "no framework-specific reference documents"
    assert sorted(path.name for path in SKILL_ROOT.iterdir()) == [
        "SKILL.md",
        "assets",
        "scripts",
    ]


def test_setup_skill_states_the_evidence_reading_rules() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "declared-only" in skill
    assert "imported-only" in skill
    assert "Multiple matches are all reported" in skill
    assert "`replaced_by`" in skill
    assert "never blocks setup and never permits a guessed" in skill
    assert "never selects a production target and never reports readiness" in skill


def test_setup_skill_scopes_documentation_lookup_to_detected_frameworks() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "source inspection confirms is relevant, and only for those" in skill
    assert "version-matched official documentation" in skill
    assert "installed package source and type signatures read-only" in skill
    assert "Cite every source used in the proposal" in skill
    assert "actual control flow is authoritative" in skill
    assert "stating that version uncertainty explicitly" in skill
    assert "Do not load documentation for undetected frameworks" in skill
    assert "context7" not in skill.lower()
    assert "cannot wire" in skill


@pytest.mark.parametrize(
    "skill_name",
    ["kensa-evals", "kensa-setup", "kensa-inspect", "kensa-generate", "kensa-diagnose"],
)
def test_packaged_skills_pass_agent_skills_frontmatter_validation(skill_name: str) -> None:
    skill_path = SKILL_ROOT.parent / skill_name / "SKILL.md"
    lines = skill_path.read_text(encoding="utf-8").splitlines()

    assert lines[0] == "---"
    closing = lines.index("---", 1)
    frontmatter = "\n".join(lines[1:closing])
    name = frontmatter.split("name:", 1)[1].split("\n", 1)[0].strip()
    description = frontmatter.split("description:", 1)[1].strip()

    assert name == skill_name
    assert len(name) <= 64
    assert 0 < len(description) <= 1024
    assert lines[closing + 1 :], "skill body must not be empty"


@pytest.mark.parametrize("agent_root", [".agents", ".claude", ".cursor"])
def test_kensa_init_installs_the_detector_and_registry_for_every_agent_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_root: str,
) -> None:
    from kensa.cli import main as cli_main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("kensa.cli._configure_trace_source_connection", lambda steps, source: None)
    monkeypatch.setattr(
        "kensa.cli._configure_redaction_readiness", lambda steps, source, **kwargs: None
    )

    assert cli_main(["init", "--agent", "all", "--trace-source", "local"]) == 0

    installed = tmp_path / agent_root / "skills" / "kensa-setup"
    assert (installed / "SKILL.md").read_bytes() == (SKILL_ROOT / "SKILL.md").read_bytes()
    assert (installed / "scripts" / "detect_frameworks.py").read_bytes() == (
        DETECTOR_PATH.read_bytes()
    )
    assert (installed / "assets" / "frameworks.json").read_bytes() == REGISTRY_PATH.read_bytes()


def test_kensa_init_ignores_bytecode_caches_inside_the_skill_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kensa import cli

    template_root = tmp_path / "template"
    for skill_name in cli._PACKAGED_SKILLS:
        skill_dir = template_root / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"{skill_name}\n", encoding="utf-8")
    cache = template_root / "kensa-setup" / "scripts" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "detect_frameworks.cpython-312.pyc").write_bytes(b"\x00\x61\x0d\x0d\xff")
    monkeypatch.setattr(cli, "_skill_template_root", lambda: template_root)

    written = cli._copy_skill_template_tree(tmp_path / "agent" / "kensa-evals" / "SKILL.md")

    assert not any("__pycache__" in path.as_posix() for path in written)
    assert not (tmp_path / "agent" / "kensa-setup" / "scripts").exists()
    assert not cli._template_tree_has_files(template_root / "kensa-setup" / "scripts")


def test_wheel_and_source_distribution_carry_every_skill_template_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tarfile
    import zipfile

    from hatchling.build import build_sdist, build_wheel

    template_root = SKILL_ROOT.parent
    expected = {
        path.relative_to(template_root.parent).as_posix()
        for path in template_root.rglob("*")
        if path.is_file()
    }
    assert "skill_templates/kensa-setup/scripts/detect_frameworks.py" in expected
    assert "skill_templates/kensa-setup/assets/frameworks.json" in expected

    monkeypatch.chdir(PROJECT_ROOT)
    wheel_name = build_wheel(str(tmp_path))
    sdist_name = build_sdist(str(tmp_path))

    with zipfile.ZipFile(tmp_path / wheel_name) as wheel:
        wheel_names = {name.removeprefix("kensa/") for name in wheel.namelist()}
    with tarfile.open(tmp_path / sdist_name) as sdist:
        sdist_names = {name.split("/src/kensa/", 1)[-1] for name in sdist.getnames()}

    assert expected <= wheel_names
    assert expected <= sdist_names
