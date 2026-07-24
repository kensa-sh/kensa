"""Reliability, cost, and latency scoring for Kensa eval artifacts."""

from __future__ import annotations

import math
import statistics
from typing import Any

from kensa._smoke import is_smoke_trial

Json = dict[str, Any]
_ELIGIBLE_STATUSES = frozenset({"pass", "fail", "error"})
_INFRASTRUCTURE_ERROR_KINDS = frozenset({"infrastructure", "setup", "teardown"})


def pass_hat_k(successes: int, total: int, k: int) -> float | None:
    """Estimate the chance that all k sampled trials pass."""
    if k <= 0 or total < k:
        return None
    if successes < k:
        return 0.0
    return math.comb(successes, k) / math.comb(total, k)


def pass_k_curve(per_cohort: list[tuple[int, int]]) -> list[Json]:
    """Average pass^k across case cohorts with enough trials."""
    if not per_cohort:
        return []
    curve: list[Json] = []
    for k in range(1, max(total for _, total in per_cohort) + 1):
        values = [
            value
            for value in (pass_hat_k(passed, total, k) for passed, total in per_cohort)
            if value is not None
        ]
        if values:
            curve.append({"k": k, "value": sum(values) / len(values), "cohorts": len(values)})
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
    known_costs = [cost for _, _, cost in cost_observations if cost is not None]
    cost_relevant_trials = len(cost_observations)
    cost_known_trials = sum(complete for _, complete, _ in cost_observations)
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
        "cost_partial": bool(known_costs) and not cost_complete,
    }


def _cost_observation(trial: Json) -> tuple[bool, bool, float | None]:
    trace = trial.get("trace")
    if not isinstance(trace, dict):
        return False, False, None
    cost = _finite_cost(trace.get("cost_usd"))
    known_cost = _finite_cost(trace.get("known_cost_usd"))
    turns = _finite_float(trace.get("llm_turns"))
    availability = trace.get("cost_available")
    operation = trial.get("active_operation")
    llm_timed_out = (
        trial.get("error_kind") == "timeout"
        and isinstance(operation, dict)
        and operation.get("kind") == "llm"
    )
    relevant = (
        (turns is not None and turns > 0)
        or availability is True
        or known_cost is not None
        or (cost is not None and cost != 0)
        or llm_timed_out
    )
    if not relevant:
        return False, False, None
    if llm_timed_out:
        return True, False, known_cost if known_cost is not None else cost
    if availability is True:
        return True, cost is not None, known_cost if known_cost is not None else cost
    if availability is False:
        return True, False, known_cost
    if "known_cost_usd" in trace:
        return True, False, known_cost
    legacy_cost = cost if cost not in {None, 0.0} else None
    return True, legacy_cost is not None, legacy_cost


def _finite_cost(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    number = _finite_float(value)
    return number if number is not None and number >= 0 else None


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
    trials = [
        trial
        for trial in _scored_trials(data)
        if trial.get("status") in _ELIGIBLE_STATUSES
        and trial.get("error_kind") not in _INFRASTRUCTURE_ERROR_KINDS
    ]
    cohorts: dict[str, Json] = {}
    for trial in trials:
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
    per_cohort = [(cohort["passed"], cohort["total"]) for cohort in cohort_values]
    return {
        "pass_k_curve": pass_k_curve(per_cohort),
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
