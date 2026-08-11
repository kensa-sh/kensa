"""Shared JSON-serialization helpers."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from pydantic import BaseModel


def jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def json_value(value: Any) -> Any:
    """Return an isolated JSON value, honoring Pydantic JSON serialization."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    json.dumps(value)
    return deepcopy(value)
