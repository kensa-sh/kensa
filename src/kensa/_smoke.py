"""Internal readiness-smoke classification."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from kensa.constants import SMOKE_CASE_ID, SMOKE_NODEID_FRAGMENT

_SMOKE_NODEID = re.compile(rf"(?:.*/)?{re.escape(SMOKE_NODEID_FRAGMENT)}(?:\[.*\])?")


def is_smoke_identity(*, case_id: str, group_id: str = "", nodeid: str = "") -> bool:
    return case_id == SMOKE_CASE_ID or any(
        _SMOKE_NODEID.fullmatch(value) is not None for value in (group_id, nodeid)
    )


def is_smoke_trial(trial: Mapping[str, Any]) -> bool:
    smoke = trial.get("smoke")
    if isinstance(smoke, bool):
        return smoke
    return is_smoke_identity(
        case_id=str(trial.get("case_id") or ""),
        group_id=str(trial.get("group_id") or ""),
        nodeid=str(trial.get("nodeid") or ""),
    )


def is_smoke_aggregate(aggregate: Mapping[str, Any]) -> bool:
    smoke = aggregate.get("smoke")
    if isinstance(smoke, bool):
        return smoke
    trials = aggregate.get("trials")
    if isinstance(trials, list) and any(
        is_smoke_trial(trial) for trial in trials if isinstance(trial, dict)
    ):
        return True
    return is_smoke_identity(
        case_id=str(aggregate.get("case_id") or ""),
        group_id=str(aggregate.get("group_id") or ""),
    )


__all__ = ["is_smoke_aggregate", "is_smoke_identity", "is_smoke_trial"]
