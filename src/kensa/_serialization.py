"""Shared JSON-serialization helpers."""

from __future__ import annotations

import json
from typing import Any

from kensa.errors import KensaCaseError


def jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def require_json_serializable(value: Any) -> None:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise KensaCaseError(f"case.run(...) output must be JSON-serializable: {exc}") from exc
