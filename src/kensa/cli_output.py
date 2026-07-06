"""Shared terminal output helpers for Kensa CLI commands."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape as rich_escape

from kensa.models import CliEnvelope

CONSOLE = Console(highlight=False)
ERR_CONSOLE = Console(stderr=True, highlight=False)


def print_json_envelope(
    *,
    command: str,
    ok: bool,
    exit_code: int,
    summary: str,
    data: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    next_steps: list[str] | None = None,
) -> None:
    envelope = CliEnvelope(
        command=command,
        ok=ok,
        exit_code=exit_code,
        summary=summary,
        data=data or {},
        warnings=warnings or [],
        errors=errors or [],
        next_steps=next_steps or [],
    )
    click.echo(envelope.model_dump_json(indent=2))


def step(title: str) -> None:
    CONSOLE.print()
    CONSOLE.print(f"[bold]{rich_escape(title)}[/bold]")


def item(text: str, *, ok: bool = True, err: bool = False) -> None:
    marker = "[green]✓[/green]" if ok else "[red]✗[/red]"
    console = ERR_CONSOLE if err else CONSOLE
    console.print(f"  {marker} {rich_escape(text)}")


def notice(text: str) -> None:
    CONSOLE.print(f"  [yellow]![/yellow] {rich_escape(text)}")


@contextmanager
def wait_status(text: str) -> Iterator[None]:
    if not ERR_CONSOLE.is_terminal:
        yield
        return
    with ERR_CONSOLE.status(rich_escape(text), spinner="line"):
        yield


def print_next_steps(next_steps: list[str]) -> None:
    if not next_steps:
        return
    CONSOLE.print()
    CONSOLE.print("[bold]Next steps[/bold]")
    for next_step in next_steps:
        notice(next_step)


def display_path(path: Path) -> Path:
    return Path(os.path.relpath(path, Path.cwd()))
