"""Trace read command implementation for the Kensa CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from kensa.cli_output import (
    display_path,
    item,
    print_json_envelope,
    print_next_steps,
)
from kensa.constants import (
    TRACE_IMPORT_LATEST_SCHEMA_VERSION,
    TRACE_IMPORTS_DIR,
)
from kensa.traces import load_trace_views, trace_view_summary


def cmd_traces(args: Any) -> int:
    json_output = bool(getattr(args, "json", False))
    try:
        source_path = resolve_trace_view_source(getattr(args, "source", None))
        traces = load_trace_views(source_path)
    except ValueError as exc:
        if json_output:
            print_json_envelope(
                command=f"traces {args.traces_command}",
                ok=False,
                exit_code=1,
                summary="Could not load traces.",
                errors=[str(exc)],
                next_steps=["Import traces first or pass --source."],
            )
            return 1
        item(f"error: {exc}", ok=False, err=True)
        print_next_steps(["Import traces first or pass --source."])
        return 1

    if args.traces_command == "list":
        summaries = [trace_view_summary(trace_item) for trace_item in traces]
        if json_output:
            print_json_envelope(
                command="traces list",
                ok=True,
                exit_code=0,
                summary=f"Loaded {len(summaries)} trace(s).",
                data={
                    "source": str(display_path(source_path)),
                    "traces": summaries,
                    "count": len(summaries),
                },
            )
            return 0
        for summary in summaries:
            click.echo(summary["id"])
        return 0

    if args.traces_command == "sample":
        trace = traces[0] if traces else None
        if json_output:
            print_json_envelope(
                command="traces sample",
                ok=True,
                exit_code=0,
                summary="Loaded a sample trace." if trace else "No traces found.",
                data={"source": str(display_path(source_path)), "trace": trace},
            )
            return 0
        if traces:
            click.echo(json.dumps(traces[0], indent=2))
        return 0

    if args.traces_command == "get":
        for trace_item in traces:
            if trace_item.get("id") == args.trace_id:
                if json_output:
                    print_json_envelope(
                        command="traces get",
                        ok=True,
                        exit_code=0,
                        summary=f"Loaded trace {args.trace_id}.",
                        data={
                            "source": str(display_path(source_path)),
                            "trace_id": args.trace_id,
                            "trace": trace_item,
                        },
                    )
                    return 0
                click.echo(json.dumps(trace_item, indent=2))
                return 0
        if json_output:
            print_json_envelope(
                command="traces get",
                ok=False,
                exit_code=1,
                summary=f"Trace not found: {args.trace_id}",
                data={"source": str(display_path(source_path)), "trace_id": args.trace_id},
                errors=[f"trace not found: {args.trace_id}"],
            )
            return 1
        item(f"trace not found: {args.trace_id}", ok=False, err=True)
        return 1

    if json_output:
        print_json_envelope(
            command="traces",
            ok=False,
            exit_code=2,
            summary="Unknown traces command.",
            errors=[f"unknown traces command: {args.traces_command}"],
        )
        return 2
    return 2


def resolve_trace_view_source(source: str | None) -> Path:
    if source:
        raw = source.removeprefix("file:")
        return Path(raw)
    latest_path = TRACE_IMPORTS_DIR / "latest.json"
    if not latest_path.exists():
        raise ValueError("No latest trace import found. Import traces first or pass --source.")
    try:
        payload = json.loads(latest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Latest trace import pointer is malformed. Import traces first or pass --source."
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != TRACE_IMPORT_LATEST_SCHEMA_VERSION
    ):
        raise ValueError(
            "Latest trace import pointer is malformed. Import traces first or pass --source."
        )
    artifact = payload.get("artifact_path")
    if not isinstance(artifact, str) or not artifact:
        raise ValueError(
            "Latest trace import pointer is missing artifact_path. "
            "Import traces first or pass --source."
        )
    artifact_path = Path(artifact)
    resolved = artifact_path if artifact_path.is_absolute() else Path.cwd() / artifact_path
    if not resolved.exists():
        raise ValueError(
            f"Latest trace import artifact not found: {display_path(resolved)}. "
            "Import traces first or pass --source."
        )
    return resolved
