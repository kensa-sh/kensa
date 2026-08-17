"""Find known framework imports without executing or modifying target code."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any, TypedDict, TypeGuard, cast

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "assets" / "frameworks.json"

IGNORED_DIRECTORIES = {
    ".direnv",
    ".eggs",
    ".git",
    ".hg",
    ".idea",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "site-packages",
    "venv",
}

ENTRY_FIELDS = {"id", "kind", "distributions", "import_roots", "documentation"}


class RegistryError(Exception):
    pass


class RegistryEntry(TypedDict):
    id: str
    kind: str
    distributions: list[str]
    import_roots: list[str]
    documentation: str


class ImportEvidence(TypedDict):
    module: str
    path: str
    line: int


class Match(TypedDict):
    id: str
    kind: str
    distributions: list[str]
    documentation: str
    imports: list[ImportEvidence]


class Document(TypedDict):
    matches: list[Match]
    parse_errors: list[str]


def _is_string_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def _registry_entry(value: object) -> RegistryEntry:
    if not isinstance(value, dict) or set(value) != ENTRY_FIELDS:
        raise RegistryError
    fields = cast(dict[str, object], value)
    identifier = fields["id"]
    kind = fields["kind"]
    distributions = fields["distributions"]
    import_roots = fields["import_roots"]
    documentation = fields["documentation"]
    if not (
        isinstance(identifier, str)
        and identifier
        and kind in ("framework", "client")
        and _is_string_list(distributions)
        and _is_string_list(import_roots)
        and import_roots
        and isinstance(documentation, str)
        and documentation.startswith("https://")
    ):
        raise RegistryError
    return {
        "id": identifier,
        "kind": cast(str, kind),
        "distributions": distributions,
        "import_roots": import_roots,
        "documentation": documentation,
    }


def _load_registry() -> list[RegistryEntry]:
    try:
        value: Any = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {"entries"}:
            raise RegistryError
        raw_entries = value["entries"]
        if not isinstance(raw_entries, list):
            raise RegistryError
        entries = [_registry_entry(entry) for entry in raw_entries]
        if len({entry["id"] for entry in entries}) != len(entries):
            raise RegistryError
        return entries
    except (OSError, UnicodeError, json.JSONDecodeError, RegistryError, TypeError):
        raise RegistryError from None


def _python_files(root: Path) -> list[Path]:
    directories = [root]
    source_files: list[Path] = []
    while directories:
        directory = directories.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError:
            continue
        for path in children:
            if path.is_symlink():
                continue
            if path.is_dir() and path.name not in IGNORED_DIRECTORIES:
                directories.append(path)
            elif path.is_file() and path.suffix == ".py":
                source_files.append(path)
    return sorted(source_files, key=lambda path: path.relative_to(root).as_posix())


def _import_groups(tree: ast.AST) -> list[tuple[int, tuple[str, ...]]]:
    groups: list[tuple[int, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            groups.extend((node.lineno, (alias.name,)) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = (
                node.module,
                *(f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*"),
            )
            groups.append((node.lineno, modules))
    return groups


def _matches_root(module: str, import_roots: list[str]) -> bool:
    return any(module == root or module.startswith(f"{root}.") for root in import_roots)


def _matched_modules(modules: tuple[str, ...], entry: RegistryEntry) -> list[str]:
    matches = [module for module in modules if _matches_root(module, entry["import_roots"])]
    if matches and matches[0] == modules[0]:
        return matches[:1]
    return matches


def _scan(root: Path, entries: list[RegistryEntry]) -> Document:
    evidence: dict[str, set[tuple[str, int, str]]] = {entry["id"]: set() for entry in entries}
    parse_errors: list[str] = []
    for source_path in _python_files(root):
        relative_path = source_path.relative_to(root).as_posix()
        try:
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError):
            parse_errors.append(relative_path)
            continue
        for line, modules in _import_groups(tree):
            for entry in entries:
                for module in _matched_modules(modules, entry):
                    evidence[entry["id"]].add((relative_path, line, module))

    matches: list[Match] = []
    for entry in sorted(entries, key=lambda item: item["id"]):
        imports: list[ImportEvidence] = [
            ImportEvidence(module=module, path=path, line=line)
            for path, line, module in sorted(evidence[entry["id"]])
        ]
        if imports:
            matches.append(
                {
                    "id": entry["id"],
                    "kind": entry["kind"],
                    "distributions": sorted(set(entry["distributions"])),
                    "documentation": entry["documentation"],
                    "imports": imports,
                }
            )
    return {"matches": matches, "parse_errors": sorted(set(parse_errors))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find known Python framework imports")
    parser.add_argument("--root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    root = cast(Path, arguments.root)
    if root.is_symlink() or not root.is_dir():
        print("error: root is not a directory", file=sys.stderr)
        return 2
    try:
        entries = _load_registry()
    except RegistryError:
        print("error: framework registry is unavailable or invalid", file=sys.stderr)
        return 1
    print(json.dumps(_scan(root, entries), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
