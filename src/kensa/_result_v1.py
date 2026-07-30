"""Frozen derivation rules for kensa.result.v1 artifacts."""

from __future__ import annotations

import math
import statistics
from typing import Any

Json = dict[str, Any]

_FAILURE_CATEGORIES = (
    "agent",
    "simulator",
    "judge",
    "configuration",
    "infrastructure",
    "harness",
    "unknown",
)


def derive_v1_aggregates(trials: list[Json]) -> list[Json]:
    groups: dict[str, list[Json]] = {}
    for trial in trials:
        groups.setdefault(trial["group_id"], []).append(trial)

    aggregates: list[Json] = []
    for group_id, group_trials in sorted(groups.items()):
        all_trials = sorted(group_trials, key=lambda trial: trial["trial_index"])
        ordered = [trial for trial in all_trials if trial["status"] != "skipped"]
        if not ordered:
            continue

        total = len(ordered)
        passed = sum(trial["status"] == "pass" for trial in ordered)
        errored = sum(trial["status"] == "error" for trial in ordered)
        failed = sum(trial["status"] == "fail" for trial in ordered)
        skipped = len(all_trials) - total
        configured = max(trial["configured_trials"] for trial in all_trials)
        partial = total + skipped < configured
        timed_out = any(_failure_kind(trial) == "timeout" for trial in ordered)
        if timed_out:
            verdict = "error"
        elif partial:
            verdict = "partial"
        elif errored:
            verdict = "error"
        elif passed == total:
            verdict = "pass"
        elif failed == total:
            verdict = "fail"
        else:
            verdict = "flaky"

        aggregates.append(
            {
                "group_id": group_id,
                "case_id": ordered[0]["case_id"],
                "configured_trials": configured,
                "total": total,
                "passed": passed,
                "failed": failed,
                "errored": errored,
                "skipped": skipped,
                "partial": partial,
                "verdict": verdict,
                "trials": ordered,
                "smoke": any(trial["smoke"] for trial in all_trials),
            }
        )
    return aggregates


def derive_v1_summary(trials: list[Json]) -> Json:
    scored_trials = [trial for trial in trials if not trial["smoke"]]
    eligible_trials = [
        trial
        for trial in scored_trials
        if trial["status"] in {"pass", "fail"}
        or (trial["status"] == "error" and _failure_category(trial) == "agent")
    ]
    error_counts = dict.fromkeys(_FAILURE_CATEGORIES, 0)
    for trial in scored_trials:
        if trial["status"] != "error":
            continue
        category = _failure_category(trial)
        if category is not None:
            error_counts[category] += 1

    cohorts: dict[str, Json] = {}
    for trial in eligible_trials:
        case_id = _trial_case_id(trial)
        group_id = str(trial.get("group_id") or case_id)
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
        cohort["passed"] += trial["status"] == "pass"

    cohort_values = list(cohorts.values())
    per_cohort = [(cohort["passed"], cohort["total"]) for cohort in cohort_values]
    return {
        "pass_k_curve": _pass_k_curve(per_cohort),
        "pass_k_cohorts": cohort_values,
        "eligible_agent_trials": sum(cohort["total"] for cohort in cohort_values),
        "error_counts": error_counts,
        "excluded_error_trials": sum(
            trial["status"] == "error" and _failure_category(trial) != "agent"
            for trial in scored_trials
        ),
        "cost_latency": _cost_latency(eligible_trials),
    }


def _pass_k_curve(per_cohort: list[tuple[int, int]]) -> list[Json]:
    if not per_cohort:
        return []

    curve: list[Json] = []
    for k in range(1, max(total for _, total in per_cohort) + 1):
        values = [
            value
            for value in (_pass_hat_k(passed, total, k) for passed, total in per_cohort)
            if value is not None
        ]
        if values:
            curve.append({"k": k, "value": sum(values) / len(values), "cohorts": len(values)})
    return curve


def _pass_hat_k(successes: int, total: int, k: int) -> float | None:
    if k <= 0 or total < k:
        return None
    if successes < k:
        return 0.0
    return math.comb(successes, k) / math.comb(total, k)


def _cost_latency(trials: list[Json]) -> Json:
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
    agent_passes = sum(trial["status"] == "pass" for trial in trials)
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
    trace = trial["trace"]
    cost = _finite_cost(trace.get("cost_usd"))
    known_cost = _finite_cost(trace.get("known_cost_usd"))
    turns = _finite_float(trace.get("llm_turns"))
    availability = trace.get("cost_available")
    operation = trial.get("active_operation")
    failure = trial.get("failure")
    llm_timed_out = (
        isinstance(failure, dict)
        and failure.get("kind") == "timeout"
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
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _trace_values(trials: list[Json], key: str) -> list[float]:
    values: list[float] = []
    for trial in trials:
        trace = trial["trace"]
        value = _finite_float(trace.get(key))
        if value is not None:
            values.append(value)
    return values


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = round((percentile / 100) * (len(ordered) - 1))
    return ordered[min(index, len(ordered) - 1)]


def _failure_category(trial: Json) -> str | None:
    return trial["failure"]["category"]


def _failure_kind(trial: Json) -> Any:
    failure = trial.get("failure")
    return failure.get("kind") if isinstance(failure, dict) else None


def _trial_case_id(trial: Json) -> str:
    case = trial.get("case")
    case_id = case.get("id") if isinstance(case, dict) else None
    return str(case_id or trial.get("case_id") or trial.get("group_id") or "unknown")


__all__ = ["derive_v1_aggregates", "derive_v1_summary"]
