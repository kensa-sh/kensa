"""Read-only Python agent framework detector for Kensa setup.

Reads dependency metadata and Python source syntax from a repository and prints one
stably ordered JSON evidence document. Never imports, executes, or mutates the target,
never opens credential files, and never reaches the network.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "kensa.framework_scan.v1"
REGISTRY_PATH = Path(__file__).resolve().parent.parent / "assets" / "frameworks.json"

MAX_SOURCE_FILES = 2000
MAX_FILE_BYTES = 1_000_000

MANIFEST_FILENAMES = (
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "pdm.lock",
    "Pipfile.lock",
)
REQUIREMENTS_PATTERN = re.compile(r"^requirements.*\.txt$")
REQUIREMENT_PATTERN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)$")
TOML_LOCK_FILENAMES = ("uv.lock", "poetry.lock", "pdm.lock")
REQUIREMENT_INCLUDE_OPTIONS = ("-r", "--requirement", "-c", "--constraint")

IGNORED_DIRECTORY_NAMES = (
    ".git",
    ".hg",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".vscode",
    "__pycache__",
    "node_modules",
)
REPORTED_EXCLUDED_DIRECTORY_NAMES = (
    ".direnv",
    ".eggs",
    ".nox",
    ".tox",
    ".venv",
    "build",
    "dist",
    "env",
    "site-packages",
    "venv",
)

KIND_ORDER = {"framework": 0, "client": 1}

NOTES = {
    "call_references": (
        "Syntactic call expressions written through a directly imported framework name. "
        "A call reference is not evidence that the call runs in the deployed application."
    ),
    "declarations": (
        "Dependency metadata only. A declared distribution without a source import stays "
        "declared-only."
    ),
    "imports": (
        "Absolute import statements only. An import without a call reference stays imported-only."
    ),
    "scope": (
        "Evidence for a coding agent. This document selects no function or class, ranks no "
        "candidate, and makes no readiness or safety claim."
    ),
}


class DetectorError(Exception):
    """Raised when the detector cannot produce evidence."""


def _normalize_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _load_registry() -> dict[str, Any]:
    try:
        raw = REGISTRY_PATH.read_text(encoding="utf-8")
    except OSError as error:
        raise DetectorError(f"Framework registry is unavailable: {error}") from error
    registry: dict[str, Any] = json.loads(raw)
    return registry


class _Registry:
    def __init__(self, document: dict[str, Any]) -> None:
        self.schema_version: str = document["schema_version"]
        self.revision: int = document["revision"]
        self.entries: dict[str, dict[str, Any]] = {
            entry["id"]: entry for entry in document["entries"]
        }
        self.distributions: dict[str, list[tuple[str, str]]] = {}
        self.import_roots: dict[str, list[str]] = {}
        for entry in document["entries"]:
            for distribution in entry["distributions"]:
                key = _normalize_distribution(distribution)
                self.distributions.setdefault(key, []).append((entry["id"], "distribution"))
            for alias in entry["aliases"]:
                key = _normalize_distribution(alias)
                self.distributions.setdefault(key, []).append((entry["id"], "alias"))
            for import_root in entry["import_roots"]:
                self.import_roots.setdefault(import_root, []).append(entry["id"])

    def match_distribution(self, name: str) -> list[tuple[str, str]]:
        return self.distributions.get(_normalize_distribution(name), [])

    def match_module(self, module: str) -> list[tuple[str, str]]:
        matches: list[tuple[str, str]] = []
        for import_root, entry_ids in self.import_roots.items():
            if module == import_root or module.startswith(f"{import_root}."):
                matches.extend((entry_id, import_root) for entry_id in entry_ids)
        return matches


class _Scan:
    def __init__(self, root: Path, registry: _Registry) -> None:
        self.root = root
        self.registry = registry
        self.exclusions: list[dict[str, str]] = []
        self.gaps: list[dict[str, str | None]] = []
        self._gap_keys: set[tuple[str, str | None, str]] = set()
        self.manifest_paths: frozenset[str] = frozenset()
        self.declarations: list[dict[str, Any]] = []
        self.imports: list[dict[str, Any]] = []
        self.call_references: list[dict[str, Any]] = []

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def exclude(self, kind: str, path: Path) -> None:
        self.exclusions.append({"kind": kind, "path": self.relative(path)})

    def gap(self, kind: str, detail: str, path: str | None = None) -> None:
        key = (kind, path, detail)
        if key in self._gap_keys:
            return
        self._gap_keys.add(key)
        self.gaps.append({"kind": kind, "path": path, "detail": detail})


def _is_inside(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def _collect_paths(scan: _Scan) -> tuple[list[Path], list[Path]]:
    manifests: list[Path] = []
    sources: list[Path] = []
    directories = [scan.root]
    while directories:
        directory = directories.pop()
        for entry in directory.iterdir():
            if entry.is_symlink():
                inside = _is_inside(entry.resolve(), scan.root)
                kind = "symlink_inside_repository" if inside else "symlink_outside_repository"
                scan.exclude(kind, entry)
                continue
            if entry.is_dir():
                if entry.name in IGNORED_DIRECTORY_NAMES:
                    continue
                if entry.name in REPORTED_EXCLUDED_DIRECTORY_NAMES:
                    scan.exclude("excluded_directory", entry)
                    continue
                directories.append(entry)
                continue
            if entry.name in MANIFEST_FILENAMES or REQUIREMENTS_PATTERN.match(entry.name):
                manifests.append(entry)
            elif entry.suffix == ".py":
                sources.append(entry)
    manifests.sort(key=scan.relative)
    sources.sort(key=scan.relative)
    if len(sources) > MAX_SOURCE_FILES:
        scan.gap(
            "source_file_limit",
            f"Scanned the first {MAX_SOURCE_FILES} of {len(sources)} Python files in path order.",
        )
        sources = sources[:MAX_SOURCE_FILES]
    return manifests, sources


def _read_text(scan: _Scan, path: Path) -> str | None:
    relative = scan.relative(path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            scan.gap("file_too_large", f"File exceeds {MAX_FILE_BYTES} bytes.", relative)
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        scan.gap("unreadable_file", f"{type(error).__name__}: {error}", relative)
        return None


def _record_declaration(
    scan: _Scan,
    name: str,
    source: str,
    specifier: str | None,
    version: str | None,
) -> None:
    for entry_id, match_kind in scan.registry.match_distribution(name):
        scan.declarations.append(
            {
                "id": entry_id,
                "kind": scan.registry.entries[entry_id]["kind"],
                "distribution": _normalize_distribution(name),
                "registry_match": match_kind,
                "source": source,
                "specifier": specifier or None,
                "version": version,
            }
        )


def _parse_requirement(line: str) -> tuple[str, str | None] | None:
    text = line.split("#", 1)[0].split(";", 1)[0].strip()
    if not text or text.startswith("-"):
        return None
    text = text.split("@", 1)[0].strip()
    match = REQUIREMENT_PATTERN.match(text)
    if match is None:
        return None
    return match.group(1), match.group(2).strip() or None


def _requirement_include(text: str) -> tuple[str, str] | None:
    for option in REQUIREMENT_INCLUDE_OPTIONS:
        if text == option:
            return option, ""
        for separator in (" ", "="):
            if text.startswith(f"{option}{separator}"):
                return option, text[len(option) + 1 :].strip()
    return None


def _record_requirement_include(
    scan: _Scan,
    path: Path,
    source: str,
    option: str,
    ref: str,
) -> None:
    target = "an unnamed file"
    if ref:
        resolved = (path.parent / ref).resolve()
        if _is_inside(resolved, scan.root):
            relative = resolved.relative_to(scan.root).as_posix()
            if relative in scan.manifest_paths:
                return
            target = relative
        else:
            target = "a file outside the repository"
    scan.gap(
        "unsupported_requirement_include",
        f"Includes {option} {target}, which this scan does not follow. "
        "Declarations in that file are missing from this evidence.",
        source,
    )


def _collect_requirements(scan: _Scan, path: Path, text: str) -> None:
    source = scan.relative(path)
    for line in text.splitlines():
        include = _requirement_include(line.split("#", 1)[0].strip())
        if include is not None:
            _record_requirement_include(scan, path, source, include[0], include[1])
            continue
        requirement = _parse_requirement(line)
        if requirement is None:
            continue
        _record_declaration(scan, requirement[0], source, requirement[1], None)


def _load_toml(scan: _Scan, path: Path, text: str) -> dict[str, Any] | None:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        scan.gap("manifest_parse_error", f"TOMLDecodeError: {error}", scan.relative(path))
        return None


def _string_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _collect_pep508_group(scan: _Scan, source: str, value: Any) -> None:
    for item in _string_items(value):
        requirement = _parse_requirement(item)
        if requirement is not None:
            _record_declaration(scan, requirement[0], source, requirement[1], None)


def _collect_poetry_table(scan: _Scan, source: str, value: Any) -> None:
    if not isinstance(value, dict):
        return
    for name, constraint in value.items():
        if name == "python":
            continue
        specifier = constraint if isinstance(constraint, str) else None
        _record_declaration(scan, name, source, specifier, None)


def _collect_pyproject(scan: _Scan, path: Path, text: str) -> None:
    document = _load_toml(scan, path, text)
    if document is None:
        return
    source = scan.relative(path)
    project = document.get("project", {})
    if isinstance(project, dict):
        _collect_pep508_group(scan, source, project.get("dependencies"))
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in sorted(optional):
                _collect_pep508_group(scan, source, optional[group])
    groups = document.get("dependency-groups")
    if isinstance(groups, dict):
        for group in sorted(groups):
            _collect_pep508_group(scan, source, groups[group])
    tools = document.get("tool", {})
    if not isinstance(tools, dict):
        return
    poetry = tools.get("poetry")
    if isinstance(poetry, dict):
        _collect_poetry_table(scan, source, poetry.get("dependencies"))
        poetry_groups = poetry.get("group")
        if isinstance(poetry_groups, dict):
            for group in sorted(poetry_groups):
                definition = poetry_groups[group]
                if isinstance(definition, dict):
                    _collect_poetry_table(scan, source, definition.get("dependencies"))
    uv = tools.get("uv")
    if isinstance(uv, dict):
        _collect_pep508_group(scan, source, uv.get("dev-dependencies"))
    pdm = tools.get("pdm")
    if isinstance(pdm, dict):
        pdm_groups = pdm.get("dev-dependencies")
        if isinstance(pdm_groups, dict):
            for group in sorted(pdm_groups):
                _collect_pep508_group(scan, source, pdm_groups[group])


def _collect_toml_lock(scan: _Scan, path: Path, text: str) -> None:
    document = _load_toml(scan, path, text)
    if document is None:
        return
    source = scan.relative(path)
    packages = document.get("package")
    if not isinstance(packages, list):
        return
    for package in packages:
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        if not isinstance(name, str):
            continue
        version = package.get("version")
        _record_declaration(scan, name, source, None, version if isinstance(version, str) else None)


def _collect_pipenv_lock(scan: _Scan, path: Path, text: str) -> None:
    source = scan.relative(path)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        scan.gap("manifest_parse_error", f"JSONDecodeError: {error}", source)
        return
    if not isinstance(document, dict):
        return
    for section in ("default", "develop"):
        packages = document.get(section)
        if not isinstance(packages, dict):
            continue
        for name in sorted(packages):
            definition = packages[name]
            specifier = None
            if isinstance(definition, dict):
                pinned = definition.get("version")
                specifier = pinned if isinstance(pinned, str) else None
            _record_declaration(scan, name, source, specifier, None)


def _collect_manifest(scan: _Scan, path: Path) -> None:
    text = _read_text(scan, path)
    if text is None:
        return
    if path.name == "pyproject.toml":
        _collect_pyproject(scan, path, text)
    elif path.name in TOML_LOCK_FILENAMES:
        _collect_toml_lock(scan, path, text)
    elif path.name == "Pipfile.lock":
        _collect_pipenv_lock(scan, path, text)
    else:
        _collect_requirements(scan, path, text)


def _first_party_roots(root: Path) -> set[str]:
    roots: set[str] = set()
    for parent in (root, root / "src"):
        if not parent.is_dir():
            continue
        for entry in parent.iterdir():
            if entry.is_symlink():
                continue
            if entry.is_dir() and (entry / "__init__.py").is_file():
                roots.add(entry.name)
            elif entry.suffix == ".py":
                roots.add(entry.stem)
    return roots


def _target_names(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    if isinstance(node, ast.Tuple | ast.List):
        return [name for element in node.elts for name in _target_names(element)]
    return []


def _non_import_bindings(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_target_names(target))
        elif isinstance(
            node,
            ast.AnnAssign
            | ast.AugAssign
            | ast.NamedExpr
            | ast.For
            | ast.AsyncFor
            | ast.comprehension,
        ):
            names.update(_target_names(node.target))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            names.update(_target_names(node.optional_vars))
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            names.add(node.name)
        elif isinstance(node, ast.Global | ast.Nonlocal):
            names.update(node.names)
        elif isinstance(node, ast.MatchAs | ast.MatchStar) and node.name is not None:
            names.add(node.name)
    return names


def _call_reference(func: ast.expr) -> tuple[str, str] | None:
    attributes: list[str] = []
    current: ast.expr = func
    while isinstance(current, ast.Attribute):
        attributes.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    attributes.append(current.id)
    attributes.reverse()
    return current.id, ".".join(attributes)


def _resolve_module(
    scan: _Scan,
    module: str,
    first_party: set[str],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return registry matches for ``module``, or the matches a first-party module shadows."""
    matches = scan.registry.match_module(module)
    if matches and module.split(".", 1)[0] in first_party:
        return [], matches
    return matches, []


def _record_shadow_gap(
    scan: _Scan,
    module: str,
    suppressed: list[tuple[str, str]],
    relative: str,
) -> None:
    entry_ids = ", ".join(sorted({entry_id for entry_id, _ in suppressed}))
    scan.gap(
        "first_party_shadowed_import_root",
        f"Import {module} resolves to a repository-local module of the same name; "
        f"suppressed registry matches: {entry_ids}. Confirm this import by hand.",
        relative,
    )


def _collect_source(scan: _Scan, path: Path, first_party: set[str]) -> None:
    relative = scan.relative(path)
    text = _read_text(scan, path)
    if text is None:
        return
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        scan.gap("source_parse_error", f"SyntaxError: {error.msg}", relative)
        return
    bindings: dict[str, list[tuple[str, str]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                matches, suppressed = _resolve_module(scan, alias.name, first_party)
                if suppressed:
                    _record_shadow_gap(scan, alias.name, suppressed, relative)
                if not matches:
                    continue
                binding = alias.asname or alias.name.split(".", 1)[0]
                bindings.setdefault(binding, []).extend(matches)
                _record_imports(scan, matches, alias.name, None, binding, relative, node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            module_matches, module_suppressed = _resolve_module(scan, node.module, first_party)
            for alias in node.names:
                matches, suppressed = module_matches, module_suppressed
                if not matches and not suppressed:
                    matches, suppressed = _resolve_module(
                        scan, f"{node.module}.{alias.name}", first_party
                    )
                if suppressed:
                    _record_shadow_gap(scan, node.module, suppressed, relative)
                    continue
                if not matches:
                    continue
                binding = alias.asname or alias.name
                bindings.setdefault(binding, []).extend(matches)
                _record_imports(
                    scan, matches, node.module, alias.name, binding, relative, node.lineno
                )
    _collect_call_references(scan, tree, bindings, relative)


def _record_imports(
    scan: _Scan,
    matches: list[tuple[str, str]],
    module: str,
    symbol: str | None,
    binding: str,
    relative: str,
    line: int,
) -> None:
    for entry_id, import_root in matches:
        scan.imports.append(
            {
                "id": entry_id,
                "kind": scan.registry.entries[entry_id]["kind"],
                "import_root": import_root,
                "module": module,
                "symbol": symbol,
                "binding": binding,
                "path": relative,
                "line": line,
            }
        )


def _collect_call_references(
    scan: _Scan,
    tree: ast.AST,
    bindings: dict[str, list[tuple[str, str]]],
    relative: str,
) -> None:
    if not bindings:
        return
    shadowed = sorted(set(bindings) & _non_import_bindings(tree))
    for name in shadowed:
        scan.gap(
            "shadowed_import_binding",
            f"Local binding {name} also names a non-import target; call references dropped.",
            relative,
        )
        del bindings[name]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        reference = _call_reference(node.func)
        if reference is None:
            continue
        for entry_id, import_root in bindings.get(reference[0], []):
            scan.call_references.append(
                {
                    "id": entry_id,
                    "kind": scan.registry.entries[entry_id]["kind"],
                    "import_root": import_root,
                    "reference": reference[1],
                    "path": relative,
                    "line": node.lineno,
                }
            )


def _summarize(scan: _Scan) -> list[dict[str, Any]]:
    detected: dict[str, dict[str, Any]] = {}

    def bucket(entry_id: str) -> dict[str, Any]:
        entry = scan.registry.entries[entry_id]
        return detected.setdefault(
            entry_id,
            {
                "id": entry_id,
                "kind": entry["kind"],
                "name": entry["name"],
                "state": "declared_only",
                "declared": False,
                "imported": False,
                "call_referenced": False,
                "distributions": set(),
                "versions": set(),
                "specifiers": set(),
                "import_roots": set(),
                "replaced_by": entry["replaced_by"],
                "documentation": entry["documentation"],
                "repository": entry["repository"],
            },
        )

    for declaration in scan.declarations:
        record = bucket(declaration["id"])
        record["declared"] = True
        record["distributions"].add(declaration["distribution"])
        if declaration["version"] is not None:
            record["versions"].add(declaration["version"])
        if declaration["specifier"] is not None:
            record["specifiers"].add(declaration["specifier"])
    for imported in scan.imports:
        record = bucket(imported["id"])
        record["imported"] = True
        record["import_roots"].add(imported["import_root"])
    for call in scan.call_references:
        bucket(call["id"])["call_referenced"] = True

    summaries: list[dict[str, Any]] = []
    for record in detected.values():
        if record["call_referenced"]:
            record["state"] = "call_referenced"
        elif record["imported"]:
            record["state"] = "imported_only"
        for key in ("distributions", "versions", "specifiers", "import_roots"):
            record[key] = sorted(record[key])
        summaries.append(record)
    summaries.sort(key=lambda record: (KIND_ORDER[record["kind"]], record["id"]))
    return summaries


def scan_repository(root: Path) -> dict[str, Any]:
    """Return the framework evidence document for ``root``."""
    registry = _Registry(_load_registry())
    scan = _Scan(root, registry)
    manifests, sources = _collect_paths(scan)
    scan.manifest_paths = frozenset(scan.relative(manifest) for manifest in manifests)
    for manifest in manifests:
        _collect_manifest(scan, manifest)
    first_party = _first_party_roots(root)
    for source in sources:
        _collect_source(scan, source, first_party)
    scan.declarations.sort(
        key=lambda item: (
            item["id"],
            item["distribution"],
            item["source"],
            item["specifier"] or "",
            item["version"] or "",
        )
    )
    scan.imports.sort(
        key=lambda item: (item["path"], item["line"], item["id"], item["module"], item["binding"])
    )
    scan.call_references.sort(
        key=lambda item: (item["path"], item["line"], item["id"], item["reference"])
    )
    scan.exclusions.sort(key=lambda item: (item["path"], item["kind"]))
    scan.gaps.sort(key=lambda item: (item["kind"], item["path"] or "", item["detail"] or ""))
    return {
        "schema_version": SCHEMA_VERSION,
        "registry_schema_version": registry.schema_version,
        "registry_revision": registry.revision,
        "root": ".",
        "notes": NOTES,
        "scan_policy": {
            "manifest_filenames": sorted(MANIFEST_FILENAMES),
            "requirements_pattern": REQUIREMENTS_PATTERN.pattern,
            "max_source_files": MAX_SOURCE_FILES,
            "max_file_bytes": MAX_FILE_BYTES,
            "ignored_directory_names": sorted(IGNORED_DIRECTORY_NAMES),
            "reported_excluded_directory_names": sorted(REPORTED_EXCLUDED_DIRECTORY_NAMES),
            "follows_symlinks": False,
            "imports_target_code": False,
            "executes_target_code": False,
            "reads_credential_files": False,
            "network_access": False,
        },
        "detected": _summarize(scan),
        "declarations": scan.declarations,
        "imports": scan.imports,
        "call_references": scan.call_references,
        "exclusions": scan.exclusions,
        "gaps": scan.gaps,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="detect_frameworks",
        description="Print read-only Python agent framework evidence for a repository.",
    )
    parser.add_argument("--root", default=".", help="Repository root to scan (default: .)")
    arguments = parser.parse_args(argv)
    root = Path(arguments.root)
    if not root.is_dir():
        sys.stderr.write(f"Not a directory: {arguments.root}\n")
        return 2
    try:
        document = scan_repository(root.resolve())
    except DetectorError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    sys.stdout.write(json.dumps(document, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
