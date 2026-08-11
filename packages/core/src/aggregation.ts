import { z } from "zod";

import { KensaCoreError, parseInput } from "./errors.js";
import {
  failureCategories,
  parseFailure,
  type EvaluationFailure,
} from "./evaluation.js";
import { parseJsonValue, type JsonValue } from "./json.js";

const trialStatuses = [
  "pass",
  "fail",
  "error",
  "skipped",
  "provisional",
] as const;
const aggregateVerdicts = [
  "pass",
  "fail",
  "flaky",
  "error",
  "partial",
] as const;

const jsonSchema = z.custom<JsonValue>((value) =>
  validates(parseJsonValue, value),
);
const failureSchema = z.custom<EvaluationFailure>((value) =>
  validates(parseFailure, value),
);

const trialSchema = z
  .strictObject({
    nodeid: z.string().min(1),
    group_id: z.string().min(1),
    case_id: z.string().min(1),
    trial_index: z.number().int().positive(),
    configured_trials: z.number().int().positive(),
    status: z.enum(trialStatuses),
    case: z.record(z.string(), jsonSchema),
    output: jsonSchema.nullable(),
    failure: failureSchema.nullable(),
    duration_ms: z.number().finite().nonnegative(),
    trace: z.record(z.string(), jsonSchema),
    judges: z.array(z.record(z.string(), jsonSchema)),
    active_operation: z.record(z.string(), jsonSchema).nullable(),
    smoke: z.boolean(),
  })
  .superRefine((trial, context) => {
    if (trial.trial_index > trial.configured_trials) {
      addIssue(context, "trial_index", "trial index exceeds configured trials");
    }
    const requiresFailure = ["fail", "error", "skipped"].includes(trial.status);
    if (requiresFailure !== (trial.failure !== null)) {
      addIssue(
        context,
        "failure",
        "trial status contradicts failure provenance",
      );
    }
    const caseId = trial.case.id;
    if (caseId !== undefined && caseId !== trial.case_id) {
      addIssue(context, "case.id", "case ID contradicts trial case ID");
    }
  });

const trialsSchema = z.array(trialSchema);
const interruptionSchema = z
  .strictObject({
    kind: z.string().min(1),
    message: z.string(),
    nodeid: z.string().nullable().default(null),
    case_id: z.string().nullable().default(null),
    trial_index: z.number().int().positive().nullable().default(null),
    phase: z.enum(["setup", "call", "teardown"]).nullable().default(null),
  })
  .nullable();
const buildRunInputSchema = z.strictObject({
  run_id: z.string().min(1),
  complete: z.boolean(),
  interruption: interruptionSchema,
  trials: trialsSchema,
});

export type Trial = z.infer<typeof trialSchema>;
export type TrialStatus = (typeof trialStatuses)[number];
export type AggregateVerdict = (typeof aggregateVerdicts)[number];

export interface TrialAggregate {
  group_id: string;
  case_id: string;
  configured_trials: number;
  total: number;
  passed: number;
  failed: number;
  errored: number;
  skipped: number;
  partial: boolean;
  verdict: AggregateVerdict;
  trials: Trial[];
  smoke: boolean;
}

export interface ReliabilityPoint {
  k: number;
  value: number;
  cohorts: number;
}

export interface ReliabilityCohort {
  group_id: string;
  case_id: string;
  passed: number;
  total: number;
}

export interface CostLatencySummary {
  latency_p50_ms: number;
  latency_p95_ms: number;
  latency_mean_ms: number;
  total_cost_usd: number | null;
  known_cost_usd: number;
  cost_per_pass_usd: number | null;
  mean_llm_turns: number;
  cost_known_trials: number;
  cost_relevant_trials: number;
  cost_coverage: number;
  has_cost: boolean;
  cost_complete: boolean;
  cost_partial: boolean;
}

export interface RunSummary {
  pass_k_curve: ReliabilityPoint[];
  pass_k_cohorts: ReliabilityCohort[];
  eligible_agent_trials: number;
  error_counts: Record<EvaluationFailure["category"], number>;
  excluded_error_trials: number;
  cost_latency: CostLatencySummary;
}

export interface RunResult {
  schema_version: "kensa.result.v1";
  run_id: string;
  complete: boolean;
  interruption: z.infer<typeof interruptionSchema>;
  trials: Trial[];
  aggregates: TrialAggregate[];
  summary: RunSummary;
}

export function parseTrials(input: unknown): Trial[] {
  return parseInput(trialsSchema, input, "trials violate the core contract");
}

export function aggregateTrials(input: unknown): TrialAggregate[] {
  const trials = parseTrials(input).sort(compareTrials);
  validateTrialIdentities(trials);
  const groups = new Map<string, Trial[]>();
  for (const trial of trials) {
    const group = groups.get(trial.group_id) ?? [];
    group.push(trial);
    groups.set(trial.group_id, group);
  }
  return [...groups.entries()]
    .sort(([left], [right]) => compareText(left, right))
    .flatMap(([groupId, group]) => aggregateGroup(groupId, group));
}

export function summarizeTrials(input: unknown): RunSummary {
  const trials = parseTrials(input).sort(compareTrials);
  validateTrialIdentities(trials);
  const scored = trials.filter((trial) => !trial.smoke);
  const eligible = scored.filter(
    (trial) =>
      trial.status === "pass" ||
      trial.status === "fail" ||
      (trial.status === "error" && trial.failure?.category === "agent"),
  );
  const errorCounts = Object.fromEntries(
    failureCategories.map((category) => [category, 0]),
  ) as Record<EvaluationFailure["category"], number>;
  for (const trial of scored) {
    if (trial.status === "error" && trial.failure !== null) {
      errorCounts[trial.failure.category] += 1;
    }
  }
  const cohorts = reliabilityCohorts(eligible);
  return {
    pass_k_curve: passKCurve(cohorts),
    pass_k_cohorts: cohorts,
    eligible_agent_trials: cohorts.reduce(
      (total, cohort) => total + cohort.total,
      0,
    ),
    error_counts: errorCounts,
    excluded_error_trials: scored.filter(
      (trial) =>
        trial.status === "error" && trial.failure?.category !== "agent",
    ).length,
    cost_latency: costLatency(eligible),
  };
}

export function buildRunResult(input: {
  run_id: string;
  complete: boolean;
  interruption: unknown;
  trials: unknown;
}): RunResult {
  const parsed = parseInput(
    buildRunInputSchema,
    input,
    "run input violates the core contract",
  );
  const trials = parsed.trials.sort(compareTrials);
  if (parsed.complete && parsed.interruption !== null) {
    throw new KensaCoreError(
      "invalid_input",
      "complete run cannot contain an interruption",
    );
  }
  if (
    parsed.complete &&
    trials.some((trial) => trial.status === "provisional")
  ) {
    throw new KensaCoreError(
      "invalid_input",
      "complete run cannot contain provisional trials",
    );
  }
  validateTrialIdentities(trials);
  return {
    schema_version: "kensa.result.v1",
    run_id: parsed.run_id,
    complete: parsed.complete,
    interruption: parsed.interruption,
    trials,
    aggregates: aggregateTrials(trials),
    summary: summarizeTrials(trials),
  };
}

function aggregateGroup(groupId: string, group: Trial[]): TrialAggregate[] {
  const all = [...group].sort(
    (left, right) => left.trial_index - right.trial_index,
  );
  const trials = all.filter((trial) => trial.status !== "skipped");
  if (trials.length === 0) {
    return [];
  }
  const total = trials.length;
  const passed = countStatus(trials, "pass");
  const failed = countStatus(trials, "fail");
  const errored = countStatus(trials, "error");
  const skipped = all.length - total;
  const configured = Math.max(...all.map((trial) => trial.configured_trials));
  const partial = total + skipped < configured;
  return [
    {
      group_id: groupId,
      case_id: trials[0]!.case_id,
      configured_trials: configured,
      total,
      passed,
      failed,
      errored,
      skipped,
      partial,
      verdict: aggregateVerdict(trials, {
        total,
        passed,
        failed,
        errored,
        partial,
      }),
      trials,
      smoke: all.some((trial) => trial.smoke),
    },
  ];
}

function aggregateVerdict(
  trials: Trial[],
  counts: {
    total: number;
    passed: number;
    failed: number;
    errored: number;
    partial: boolean;
  },
): AggregateVerdict {
  if (trials.some((trial) => trial.failure?.kind === "timeout")) return "error";
  if (counts.partial) return "partial";
  if (counts.errored > 0) return "error";
  if (counts.passed === counts.total) return "pass";
  if (counts.failed === counts.total) return "fail";
  return "flaky";
}

function reliabilityCohorts(trials: Trial[]): ReliabilityCohort[] {
  const cohorts = new Map<string, ReliabilityCohort>();
  for (const trial of trials) {
    const cohort = cohorts.get(trial.group_id) ?? {
      group_id: trial.group_id,
      case_id: caseIdentity(trial),
      passed: 0,
      total: 0,
    };
    cohort.total += 1;
    if (trial.status === "pass") cohort.passed += 1;
    cohorts.set(trial.group_id, cohort);
  }
  return [...cohorts.values()];
}

function passKCurve(cohorts: ReliabilityCohort[]): ReliabilityPoint[] {
  const maximum = Math.max(0, ...cohorts.map((cohort) => cohort.total));
  return Array.from({ length: maximum }, (_, index) => index + 1).map((k) => {
    const values = cohorts
      .map((cohort) => passHatK(cohort.passed, cohort.total, k))
      .filter((value): value is number => value !== null);
    return { k, value: mean(values), cohorts: values.length };
  });
}

function passHatK(successes: number, total: number, k: number): number | null {
  if (total < k) return null;
  if (successes < k) return 0;
  let value = 1;
  for (let index = 0; index < k; index += 1) {
    value *= (successes - index) / (total - index);
  }
  return value;
}

function costLatency(trials: Trial[]): CostLatencySummary {
  const durations = trials.map((trial) => trial.duration_ms);
  const turns = trials
    .map((trial) => finiteNumber(trial.trace.llm_turns))
    .filter(isNumber);
  const observations = trials
    .map(costObservation)
    .filter((item) => item.relevant);
  const knownCosts = observations.map((item) => item.cost).filter(isNumber);
  const knownTrials = observations.filter((item) => item.complete).length;
  const relevantTrials = observations.length;
  const complete = relevantTrials > 0 && knownTrials === relevantTrials;
  const knownCost = knownCosts.reduce((total, cost) => total + cost, 0);
  const totalCost = complete ? knownCost : null;
  const passes = countStatus(trials, "pass");
  return {
    latency_p50_ms: median(durations),
    latency_p95_ms: percentile(durations, 95),
    latency_mean_ms: preciseMean(durations),
    total_cost_usd: totalCost,
    known_cost_usd: knownCost,
    cost_per_pass_usd:
      totalCost !== null && passes > 0 ? totalCost / passes : null,
    mean_llm_turns: preciseMean(turns),
    cost_known_trials: knownTrials,
    cost_relevant_trials: relevantTrials,
    cost_coverage: relevantTrials > 0 ? knownTrials / relevantTrials : 0,
    has_cost: knownCosts.length > 0,
    cost_complete: complete,
    cost_partial: knownCosts.length > 0 && !complete,
  };
}

function costObservation(trial: Trial): {
  relevant: boolean;
  complete: boolean;
  cost: number | null;
} {
  const cost = finiteCost(trial.trace.cost_usd);
  const known = finiteCost(trial.trace.known_cost_usd);
  const turns = finiteNumber(trial.trace.llm_turns);
  const available = trial.trace.cost_available;
  const timedOut =
    trial.failure?.kind === "timeout" && trial.active_operation?.kind === "llm";
  const relevant =
    (turns !== null && turns > 0) ||
    available === true ||
    known !== null ||
    (cost !== null && cost !== 0) ||
    timedOut;
  if (!relevant) return { relevant: false, complete: false, cost: null };
  if (timedOut) return { relevant: true, complete: false, cost: known ?? cost };
  if (available === true)
    return { relevant: true, complete: cost !== null, cost: known ?? cost };
  if (available === false)
    return { relevant: true, complete: false, cost: known };
  if ("known_cost_usd" in trial.trace) {
    return { relevant: true, complete: false, cost: known };
  }
  const legacy = cost === 0 ? null : cost;
  return { relevant: true, complete: legacy !== null, cost: legacy };
}

function validateTrialIdentities(trials: Trial[]): void {
  const nodeids = new Set<string>();
  const indexes = new Set<string>();
  const configured = new Map<string, number>();
  const caseIds = new Map<string, string>();
  for (const trial of trials) {
    if (nodeids.has(trial.nodeid))
      throw new KensaCoreError(
        "invalid_input",
        "trials contain duplicate node IDs",
      );
    nodeids.add(trial.nodeid);
    const key = `${trial.group_id}\0${trial.trial_index}`;
    if (indexes.has(key))
      throw new KensaCoreError(
        "invalid_input",
        "trials contain duplicate group trial indexes",
      );
    indexes.add(key);
    const expected = configured.get(trial.group_id) ?? trial.configured_trials;
    if (expected !== trial.configured_trials) {
      throw new KensaCoreError(
        "invalid_input",
        "trials in one group have inconsistent configured trials",
      );
    }
    configured.set(trial.group_id, expected);
    const expectedCaseId = caseIds.get(trial.group_id) ?? trial.case_id;
    if (expectedCaseId !== trial.case_id) {
      throw new KensaCoreError(
        "invalid_input",
        "trials in one group have inconsistent case IDs",
      );
    }
    caseIds.set(trial.group_id, expectedCaseId);
  }
}

function countStatus(trials: Trial[], status: TrialStatus): number {
  return trials.filter((trial) => trial.status === status).length;
}

function finiteCost(value: JsonValue | undefined): number | null {
  const number = finiteNumber(value);
  return number !== null && number >= 0 ? number : null;
}

function finiteNumber(value: JsonValue | undefined): number | null {
  if (typeof value === "boolean" || value === null || value === undefined)
    return null;
  if (typeof value !== "number" && typeof value !== "string") return null;
  const normalized = typeof value === "string" ? value.trim() : value;
  if (
    typeof normalized === "string" &&
    !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(normalized)
  ) {
    return null;
  }
  const number =
    typeof normalized === "number" ? normalized : Number(normalized);
  return Number.isFinite(number) ? number : null;
}

function percentile(values: number[], percent: number): number {
  if (values.length === 0) return 0;
  const ordered = [...values].sort((left, right) => left - right);
  const scaled = percent * (ordered.length - 1);
  const lower = Math.floor(scaled / 100);
  const remainder = scaled % 100;
  const index =
    remainder > 50 || (remainder === 50 && lower % 2 === 1) ? lower + 1 : lower;
  return ordered[index]!;
}

function median(values: number[]): number {
  if (values.length === 0) return 0;
  const ordered = [...values].sort((left, right) => left - right);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2 === 0
    ? (ordered[middle - 1]! + ordered[middle]!) / 2
    : ordered[middle]!;
}

function mean(values: number[]): number {
  return values.reduce((total, value) => total + value, 0) / values.length;
}

function preciseMean(values: number[]): number {
  if (values.length === 0) return 0;
  const partials: number[] = [];
  for (const value of values) {
    let high = value;
    let index = 0;
    for (const partial of partials) {
      let low = partial;
      if (Math.abs(high) < Math.abs(low)) {
        [high, low] = [low, high];
      }
      const sum = high + low;
      const error = low - (sum - high);
      if (error !== 0) {
        partials[index] = error;
        index += 1;
      }
      high = sum;
    }
    partials.length = index;
    partials.push(high);
  }
  return partials.reduce((total, value) => total + value, 0) / values.length;
}

function caseIdentity(trial: Trial): string {
  const id = trial.case.id;
  return typeof id === "string" && id.length > 0 ? id : trial.case_id;
}

function compareTrials(left: Trial, right: Trial): number {
  return (
    compareText(left.group_id, right.group_id) ||
    left.trial_index - right.trial_index ||
    compareText(left.nodeid, right.nodeid)
  );
}

function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function validates<T>(parser: (value: unknown) => T, value: unknown): boolean {
  try {
    parser(value);
    return true;
  } catch {
    return false;
  }
}

function isNumber(value: number | null): value is number {
  return value !== null;
}

function addIssue(
  context: z.RefinementCtx,
  path: string,
  message: string,
): void {
  context.addIssue({ code: "custom", path: [path], message });
}
