"""Reliability, cost, and latency scoring for Kensa eval artifacts."""

from __future__ import annotations

import math
import statistics
from typing import Any

from kensa._smoke import is_smoke_trial

Json = dict[str, Any]


def pass_hat_k(successes: int, total: int, k: int) -> float | None:
    """Estimate the chance that all k sampled trials pass."""
    if k <= 0 or total < k:
        return None
    if successes < k:
        return 0.0
    return math.comb(successes, k) / math.comb(total, k)


def pass_k_curve(per_case: list[tuple[int, int]]) -> list[Json]:
    """Average pass^k across case cohorts with enough trials."""
    if not per_case:
        return []
    curve: list[Json] = []
    for k in range(1, max(total for _, total in per_case) + 1):
        values = [
            value
            for value in (pass_hat_k(passed, total, k) for passed, total in per_case)
            if value is not None
        ]
        if values:
            curve.append({"k": k, "value": sum(values) / len(values), "cases": len(values)})
    return curve


def cost_latency(trials: list[Json]) -> Json:
    """Summarize recorded trial cost, latency, and LLM turns."""
    durations = [
        float(trial["duration_ms"]) for trial in trials if trial.get("duration_ms") is not None
    ]
    turns = _trace_values(trials, "llm_turns")
    cost_observations = [
        observation for trial in trials if (observation := _cost_observation(trial))[0]
    ]
    known_costs = [cost for _, cost in cost_observations if cost is not None]
    cost_relevant_trials = len(cost_observations)
    cost_known_trials = len(known_costs)
    cost_complete = cost_relevant_trials > 0 and cost_known_trials == cost_relevant_trials
    known_cost = sum(known_costs)
    total_cost = known_cost if cost_complete else None
    agent_passes = sum(1 for trial in trials if trial.get("status") == "pass")
    return {
        "latency_p50_ms": statistics.median(durations) if durations else 0.0,
        "latency_p95_ms": _percentile(durations, 95),
        "latency_mean_ms": statistics.fmean(durations) if durations else 0.0,
        "total_cost_usd": total_cost,
        "known_cost_usd": known_cost,
        "cost_per_pass_usd": (
            total_cost / agent_passes if total_cost is not None and agent_passes else None
        ),
        "mean_llm_turns": statistics.fmean(turns) if turns else 0.0,
        "cost_known_trials": cost_known_trials,
        "cost_relevant_trials": cost_relevant_trials,
        "cost_coverage": (
            cost_known_trials / cost_relevant_trials if cost_relevant_trials else 0.0
        ),
        "has_cost": bool(known_costs),
        "cost_complete": cost_complete,
        "cost_partial": 0 < cost_known_trials < cost_relevant_trials,
    }


def _cost_observation(trial: Json) -> tuple[bool, float | None]:
    trace = trial.get("trace")
    if not isinstance(trace, dict):
        return False, None
    cost = _finite_float(trace.get("cost_usd"))
    turns = _finite_float(trace.get("llm_turns"))
    availability = trace.get("cost_available")
    relevant = (
        (turns is not None and turns > 0)
        or availability is True
        or (cost is not None and cost != 0)
    )
    if not relevant:
        return False, None
    if availability is True:
        return True, cost
    if availability is False:
        return True, None
    return True, cost if cost not in {None, 0.0} else None


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _trace_values(trials: list[Json], key: str) -> list[float]:
    values: list[float] = []
    for trial in trials:
        trace = trial.get("trace")
        if not isinstance(trace, dict):
            continue
        value = trace.get(key)
        if value is not None:
            values.append(float(value))
    return values


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = round((percentile / 100) * (len(ordered) - 1))
    return ordered[min(index, len(ordered) - 1)]


def run_summary(data: Json) -> Json:
    """Summarize reliability and performance for one eval artifact."""
    trials = _scored_trials(data)
    cohorts: dict[str, Json] = {}
    for trial in trials:
        if trial.get("error_kind") == "infrastructure":
            continue
        if trial.get("status") not in {"pass", "fail", "error"}:
            continue
        case_id = _trial_case_id(trial)
        group_id = _trial_group_id(trial, case_id=case_id)
        cohort = cohorts.setdefault(
            group_id,
            {
                "group_id": group_id,
                "case_id": case_id,
                "passed": 0,
                "total": 0,
            },
        )
        cohort["total"] += 1
        cohort["passed"] += trial.get("status") == "pass"

    cohort_values = list(cohorts.values())
    per_case = [(cohort["passed"], cohort["total"]) for cohort in cohort_values]
    return {
        "pass_k_curve": pass_k_curve(per_case),
        "pass_k_cohorts": cohort_values,
        "eligible_agent_trials": sum(cohort["total"] for cohort in cohort_values),
        "cost_latency": cost_latency(trials),
    }


def _scored_trials(data: Json) -> list[Json]:
    rows = data.get("trials")
    if not isinstance(rows, list):
        return []
    return [trial for trial in rows if isinstance(trial, dict) and not is_smoke_trial(trial)]


def _trial_group_id(trial: Json, *, case_id: str) -> str:
    return str(trial.get("group_id") or case_id)


def _trial_case_id(trial: Json) -> str:
    case = trial.get("case")
    case_id = case.get("id") if isinstance(case, dict) else None
    return str(case_id or trial.get("case_id") or trial.get("group_id") or "unknown")


__all__ = ["cost_latency", "pass_hat_k", "pass_k_curve", "run_summary"]
