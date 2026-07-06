"""Inspect queue command implementation for the Kensa CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import yaml
from pydantic import ValidationError

from kensa.cli_output import display_path, item, notice, print_json_envelope, print_next_steps
from kensa.cli_traces import resolve_trace_view_source
from kensa.constants import INSPECT_DIR
from kensa.models import InspectQueue
from kensa.traces import load_trace_views

_LINT_FIX_STEP = "Fix the reported queue files and rerun kensa inspect lint."


@dataclass(frozen=True)
class LoadedInspectQueue:
    path: Path
    queue: InspectQueue


def load_inspect_queues(
    inspect_dir: Path | str = INSPECT_DIR,
) -> tuple[list[LoadedInspectQueue], list[str], list[str]]:
    root = Path(inspect_dir)
    loaded: list[LoadedInspectQueue] = []
    errors: list[str] = []
    warnings: list[str] = []
    if not root.exists():
        return loaded, errors, warnings
    warnings.extend(
        f"legacy markdown queue ignored: {display_path(legacy_path)}"
        for legacy_path in sorted(root.glob("*.md"))
    )
    for path in sorted([*root.glob("*.yaml"), *root.glob("*.yml")]):
        try:
            payload = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{display_path(path)}: invalid YAML: {exc}")
            continue
        try:
            queue = InspectQueue.model_validate(payload)
        except ValidationError as exc:
            errors.append(f"{display_path(path)}: {exc}")
            continue
        loaded.append(LoadedInspectQueue(path=path, queue=queue))
    errors.extend(_cross_file_duplicate_ids(loaded))
    return loaded, errors, warnings


def _cross_file_duplicate_ids(loaded: list[LoadedInspectQueue]) -> list[str]:
    id_files: dict[str, list[str]] = {}
    for entry in loaded:
        for idea in entry.queue.items:
            id_files.setdefault(idea.id, []).append(str(display_path(entry.path)))
    return [
        f"duplicate inspect item id across files: {item_id} ({', '.join(paths)})"
        for item_id, paths in sorted(id_files.items())
        if len(paths) > 1
    ]


def _trace_reference_warnings(loaded: list[LoadedInspectQueue]) -> list[str]:
    referenced = sorted(
        {trace_id for entry in loaded for idea in entry.queue.items for trace_id in idea.trace_ids}
    )
    if not referenced:
        return []
    try:
        traces = load_trace_views(resolve_trace_view_source(None))
    except ValueError as exc:
        return [f"could not verify trace ids against latest import: {exc}"]
    known = {trace.get("id") for trace in traces}
    return [
        f"trace id not found in latest import: {trace_id}"
        for trace_id in referenced
        if trace_id not in known
    ]


def cmd_inspect(args: Any) -> int:
    json_output = bool(getattr(args, "json", False))
    loaded, errors, warnings = load_inspect_queues()

    if args.inspect_command == "lint":
        return _cmd_lint(loaded, errors, warnings, json_output=json_output)
    if args.inspect_command == "list":
        status = getattr(args, "status", None)
        return _cmd_list(loaded, errors, warnings, status=status, json_output=json_output)

    if json_output:
        print_json_envelope(
            command="inspect",
            ok=False,
            exit_code=2,
            summary="Unknown inspect command.",
            errors=[f"unknown inspect command: {args.inspect_command}"],
        )
        return 2
    item(f"unknown inspect command: {args.inspect_command}", ok=False, err=True)
    return 2


def _cmd_lint(
    loaded: list[LoadedInspectQueue],
    errors: list[str],
    warnings: list[str],
    *,
    json_output: bool,
) -> int:
    warnings = warnings + _trace_reference_warnings(loaded)
    ok = not errors
    exit_code = 0 if ok else 1
    total_items = sum(len(entry.queue.items) for entry in loaded)
    if json_output:
        print_json_envelope(
            command="inspect lint",
            ok=ok,
            exit_code=exit_code,
            summary=(
                f"Validated {len(loaded)} queue file(s) with {total_items} item(s)."
                if ok
                else "Inspect queue validation failed."
            ),
            data={
                "files": [str(display_path(entry.path)) for entry in loaded],
                "item_count": total_items,
            },
            warnings=warnings,
            errors=errors,
            next_steps=[] if ok else [_LINT_FIX_STEP],
        )
        return exit_code
    for entry in loaded:
        item(f"{display_path(entry.path)}: {len(entry.queue.items)} item(s)")
    for warning in warnings:
        notice(warning)
    for error in errors:
        item(error, ok=False, err=True)
    if not ok:
        print_next_steps([_LINT_FIX_STEP])
    return exit_code


def _cmd_list(
    loaded: list[LoadedInspectQueue],
    errors: list[str],
    warnings: list[str],
    *,
    status: str | None,
    json_output: bool,
) -> int:
    ideas = [
        {"file": str(display_path(entry.path)), **idea.model_dump(mode="json")}
        for entry in loaded
        for idea in entry.queue.items
        if status is None or idea.status == status
    ]
    ok = not errors
    exit_code = 0 if ok else 1
    if json_output:
        print_json_envelope(
            command="inspect list",
            ok=ok,
            exit_code=exit_code,
            summary=f"Loaded {len(ideas)} inspect item(s).",
            data={"items": ideas, "count": len(ideas)},
            warnings=warnings,
            errors=errors,
            next_steps=[] if ok else [_LINT_FIX_STEP],
        )
        return exit_code
    for idea in ideas:
        click.echo(f"{idea['id']} {idea['status']}")
    for error in errors:
        item(error, ok=False, err=True)
    if not ok:
        print_next_steps([_LINT_FIX_STEP])
    return exit_code
