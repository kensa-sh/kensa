"""Kensa project configuration in pyproject.toml."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from kensa.models import KensaProjectConfig

_CONFIG_KEYS = frozenset({"dotenv", "evidence_source", "redaction_model"})
_PROJECT_KEYS = frozenset({"evidence_source", "redaction_model"})
_KENSA_HEADER = re.compile(r"^\s*\[tool\.kensa\]\s*(?:#.*)?$")
_TABLE_HEADER = re.compile(r"^\s*\[\[?.+?\]\]?\s*(?:#.*)?$")


class KensaConfigError(ValueError):
    """Raised when project configuration cannot be read or updated safely."""


@dataclass(frozen=True)
class ProjectConfigWrite:
    path: Path
    created: bool
    changed: bool


@dataclass(frozen=True)
class _TomlScanState:
    depth: int = 0
    quote: str | None = None


def find_pyproject(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        path = candidate / "pyproject.toml"
        if path.exists():
            return path
    return None


def read_project_config(start: Path | str | None = None) -> KensaProjectConfig:
    path = find_pyproject(Path(start) if start is not None else Path.cwd())
    if path is None:
        return KensaProjectConfig()
    table = _read_kensa_table(path, strict=True)
    payload = {key: table[key] for key in _PROJECT_KEYS if key in table}
    try:
        return KensaProjectConfig.model_validate(payload)
    except ValidationError as exc:
        raise KensaConfigError(f"invalid Kensa configuration in {path}") from exc


def read_dotenv_path(pyproject: Path) -> Path | None:
    try:
        table = _read_kensa_table(pyproject, strict=False)
    except KensaConfigError:
        return None
    declared = table.get("dotenv")
    if not isinstance(declared, str) or not declared:
        return None
    return Path(declared).expanduser()


def update_project_config(
    updates: Mapping[str, str],
    *,
    start: Path | str | None = None,
) -> ProjectConfigWrite:
    root = Path(start) if start is not None else Path.cwd()
    path = find_pyproject(root) or root.resolve() / "pyproject.toml"
    if not updates:
        return ProjectConfigWrite(path=path, created=False, changed=False)
    created = not path.exists()
    existing = _read_kensa_table(path, strict=True) if path.exists() else {}
    _validate_updates(existing, updates, path)
    source = path.read_bytes().decode() if path.exists() else ""
    has_section = _find_table_header(_source_lines(source), _KENSA_HEADER) is not None
    has_config = has_section
    if not has_section and source:
        tool = tomllib.loads(source).get("tool")
        has_config = isinstance(tool, dict) and "kensa" in tool
    if has_config and all(existing.get(key) == value for key, value in updates.items()):
        return ProjectConfigWrite(path=path, created=False, changed=False)
    if has_config and not has_section and not _can_declare_kensa_parent(source):
        raise KensaConfigError(
            f"cannot update Kensa configuration in {path}: use a [tool.kensa] table"
        )
    rendered = _update_source(source, updates)
    _validate_rendered_config(rendered, updates, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(rendered.encode())
    return ProjectConfigWrite(path=path, created=created, changed=True)


def _read_kensa_table(path: Path, *, strict: bool) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_bytes().decode())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise KensaConfigError(f"invalid TOML in {path}") from exc
    tool = data.get("tool")
    if tool is None:
        return {}
    if not isinstance(tool, dict):
        if strict:
            raise KensaConfigError(f"[tool] must be a table in {path}")
        return {}
    kensa = tool.get("kensa")
    if kensa is None:
        return {}
    if not isinstance(kensa, dict):
        if strict:
            raise KensaConfigError(f"[tool.kensa] must be a table in {path}")
        return {}
    return kensa


def _validate_updates(existing: Mapping[str, Any], updates: Mapping[str, str], path: Path) -> None:
    unsupported = set(updates) - _CONFIG_KEYS
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise KensaConfigError(f"unsupported Kensa configuration keys: {names}")
    dotenv = updates.get("dotenv", existing.get("dotenv"))
    if dotenv is not None and (not isinstance(dotenv, str) or not dotenv):
        raise KensaConfigError(f"invalid Kensa configuration in {path}")
    payload = {
        key: updates[key] if key in updates else existing[key]
        for key in _PROJECT_KEYS
        if key in updates or key in existing
    }
    try:
        KensaProjectConfig.model_validate(payload)
    except ValidationError as exc:
        raise KensaConfigError(f"invalid Kensa configuration in {path}") from exc


def _update_source(source: str, updates: Mapping[str, str]) -> str:
    if not source:
        lines = ["[tool.kensa]", *(_assignment(key, value) for key, value in updates.items())]
        return "\n".join(lines) + "\n"
    line_ending = _line_ending(source)
    lines = _source_lines(source)
    header = _find_table_header(lines, _KENSA_HEADER)
    if header is None:
        separator = "" if source.endswith(("\n\n", "\r\n\r\n")) else line_ending
        section = line_ending.join(
            ["[tool.kensa]", *(_assignment(key, value) for key, value in updates.items())]
        )
        terminator = "" if source.endswith("\n") else line_ending
        return f"{source}{terminator}{separator}{section}{line_ending}"
    end = _section_end(lines, header + 1)
    missing: list[tuple[str, str]] = []
    for key, value in updates.items():
        rendered_key = re.escape(key)
        assignment = re.compile(rf"^\s*(?:{rendered_key}|\"{rendered_key}\"|'{rendered_key}')\s*=")
        index = next(
            (
                candidate
                for candidate in range(header + 1, end)
                if assignment.match(lines[candidate])
            ),
            None,
        )
        if index is None:
            missing.append((key, value))
        else:
            lines[index] = _replace_value(lines[index], value)
    insertion = end
    while insertion > header + 1 and not lines[insertion - 1].strip():
        insertion -= 1
    lines[insertion:insertion] = [_assignment(key, value) for key, value in missing]
    final_ending = line_ending if source.endswith("\n") else ""
    return line_ending.join(lines) + final_ending


def _line_ending(source: str) -> str:
    crlf = source.count("\r\n")
    lf = source.count("\n") - crlf
    if crlf and crlf >= lf:
        return "\r\n"
    return "\n"


def _source_lines(source: str) -> list[str]:
    lines = re.split(r"\r\n|\n", source)
    if source.endswith("\n"):
        lines.pop()
    return lines


def _can_declare_kensa_parent(source: str) -> bool:
    line_ending = _line_ending(source)
    separator = "" if source.endswith("\n") else line_ending
    try:
        tomllib.loads(f"{source}{separator}[tool.kensa]{line_ending}")
    except tomllib.TOMLDecodeError:
        return False
    return True


def _find_table_header(
    lines: list[str],
    pattern: re.Pattern[str],
    start: int = 0,
) -> int | None:
    state = _TomlScanState()
    for index in range(start, len(lines)):
        line = lines[index]
        if state.depth == 0 and state.quote is None and pattern.match(line):
            return index
        state = _scan_toml_line(line, state)
    return None


def _section_end(lines: list[str], start: int) -> int:
    end = _find_table_header(lines, _TABLE_HEADER, start)
    return len(lines) if end is None else end


def _scan_toml_line(line: str, state: _TomlScanState) -> _TomlScanState:
    depth = state.depth
    quote = state.quote
    escaped = False
    index = 0
    while index < len(line):
        character = line[index]
        if escaped:
            escaped = False
        elif quote in {"'''", '"""'}:
            if line.startswith(quote, index):
                quote = None
                index += 2
            elif character == "\\" and quote == '"""':
                escaped = True
        elif quote is not None:
            if character == quote:
                quote = None
            elif character == "\\" and quote == '"':
                escaped = True
        elif line.startswith(("'''", '"""'), index):
            quote = line[index : index + 3]
            index += 2
        elif character in {"'", '"'}:
            quote = character
        elif character == "#":
            break
        elif character in "[{":
            depth += 1
        elif character in "]}":
            depth = max(0, depth - 1)
        index += 1
    return _TomlScanState(depth=depth, quote=quote)


def _assignment(key: str, value: str) -> str:
    return f"{key} = {json.dumps(value)}"


def _validate_rendered_config(
    rendered: str,
    updates: Mapping[str, str],
    path: Path,
) -> None:
    try:
        data = tomllib.loads(rendered)
        tool = data.get("tool")
        kensa = tool.get("kensa") if isinstance(tool, dict) else None
        matches = isinstance(kensa, dict) and all(
            kensa.get(key) == value for key, value in updates.items()
        )
    except tomllib.TOMLDecodeError as exc:
        raise KensaConfigError(f"could not safely update Kensa configuration in {path}") from exc
    if not matches:
        raise KensaConfigError(f"could not safely update Kensa configuration in {path}")


def _replace_value(line: str, value: str) -> str:
    equals = line.index("=")
    prefix = line[: equals + 1]
    remainder = line[equals + 1 :]
    comment = _comment_index(remainder)
    value_text = remainder if comment is None else remainder[:comment]
    suffix = "" if comment is None else remainder[comment:]
    leading = value_text[: len(value_text) - len(value_text.lstrip())]
    trailing = value_text[len(value_text.rstrip()) :]
    return f"{prefix}{leading}{json.dumps(value)}{trailing}{suffix}"


def _comment_index(value: str) -> int | None:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote == '"':
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "#":
            return index
    return None
