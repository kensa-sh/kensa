import { z } from "zod";

import { KensaCoreError, parseInput } from "./errors.js";
import {
  canonicalJson,
  jsonObjectSchema,
  jsonValueSchema,
  type JsonValue,
} from "./json.js";

export const failureCategories = [
  "agent",
  "configuration",
  "harness",
  "infrastructure",
  "judge",
  "simulator",
  "unknown",
] as const;

const failureSchema = z.strictObject({
  category: z.enum(failureCategories),
  kind: z.string().trim().min(1),
  message: z.string().trim().min(1),
  evidence: jsonObjectSchema,
});

const caseSchema = z.strictObject({
  id: z.string().trim().min(1),
  input: jsonValueSchema,
  metadata: jsonObjectSchema,
});

const traceSchema = z
  .strictObject({
    spans: z.array(jsonValueSchema),
    agent_runs: z.array(jsonValueSchema),
    tools: z.array(z.string()),
    tool_calls: z.array(jsonValueSchema),
    incomplete: z.boolean(),
    incomplete_reason: z.string().trim().min(1).nullable(),
    duration_ms: z.number().finite().nonnegative(),
    cost_usd: z.number().finite().nonnegative().nullable(),
    known_cost_usd: z.number().finite().nonnegative().nullable(),
    cost_available: z.boolean(),
    llm_turns: z.number().int().nonnegative(),
  })
  .superRefine((trace, context) => {
    if (trace.incomplete === (trace.incomplete_reason === null)) {
      addIssue(
        context,
        "incomplete_reason",
        "incomplete evidence and its reason must agree",
      );
    }
    if (trace.cost_available === (trace.cost_usd === null)) {
      addIssue(
        context,
        "cost_usd",
        "cost availability and total cost must agree",
      );
    }
    if (
      trace.cost_available &&
      trace.known_cost_usd !== null &&
      trace.known_cost_usd !== trace.cost_usd
    ) {
      addIssue(
        context,
        "known_cost_usd",
        "known cost must equal total cost when cost is available",
      );
    }
  });

const observationSchema = z
  .strictObject({
    output: jsonValueSchema.nullable(),
    output_recorded: z.boolean(),
    trace: traceSchema,
    failure: failureSchema.nullable(),
  })
  .superRefine((observation, context) => {
    if (!observation.output_recorded && observation.output !== null) {
      addIssue(
        context,
        "output",
        "unrecorded output must use the null wire representation",
      );
    }
  });

export const checkOutcomes = [
  "satisfied",
  "unsatisfied",
  "error",
  "skipped",
] as const;

const checkSchema = z
  .strictObject({
    id: z.string().trim().min(1),
    outcome: z.enum(checkOutcomes),
    failure: failureSchema.nullable(),
  })
  .superRefine((check, context) => {
    const requiresFailure = check.outcome !== "satisfied";
    if (requiresFailure !== (check.failure !== null)) {
      addIssue(
        context,
        "failure",
        `${check.outcome} check has contradictory failure provenance`,
      );
      return;
    }
    if (check.failure === null) {
      return;
    }
    const allowed = allowedOutcomes(check.failure.category);
    if (!allowed.includes(check.outcome)) {
      addIssue(
        context,
        "outcome",
        `${check.failure.category} failure cannot produce ${check.outcome}`,
      );
    }
  });
const checksSchema = z
  .array(checkSchema)
  .min(1)
  .superRefine((checks, context) => {
    const ids = new Set<string>();
    for (const [index, check] of checks.entries()) {
      if (ids.has(check.id)) {
        context.addIssue({
          code: "custom",
          path: [index, "id"],
          message: "evaluation checks contain duplicate IDs",
        });
      }
      ids.add(check.id);
    }
  });

const cancellationReasonSchema = z.string().trim().min(1);

export type EvaluationCase = z.infer<typeof caseSchema>;
export type EvaluationFailure = z.infer<typeof failureSchema>;
export type EvaluationObservation = z.infer<typeof observationSchema>;
export type EvaluationCheck = z.infer<typeof checkSchema>;
export type EvaluationVerdict = "pass" | "fail" | "error" | "skipped";
export type EvaluationAction = "invoke_agent" | "evaluate_check";

export interface AwaitingObservation {
  phase: "awaiting_observation";
  case: EvaluationCase;
}

export interface AwaitingCheck {
  phase: "awaiting_check";
  case: EvaluationCase;
  observation: EvaluationObservation;
}

export interface Complete {
  phase: "complete";
  case: EvaluationCase;
  observation: EvaluationObservation;
  check: EvaluationCheck;
  failure: EvaluationFailure | null;
  verdict: EvaluationVerdict;
}

export interface MultiCheckComplete {
  phase: "complete";
  case: EvaluationCase;
  observation: EvaluationObservation;
  checks: EvaluationCheck[];
  failure: EvaluationFailure | null;
  verdict: EvaluationVerdict;
}

export interface Cancelled {
  phase: "cancelled";
  case: EvaluationCase;
  observation: EvaluationObservation | null;
  reason: string;
  failure: EvaluationFailure;
  verdict: "error";
}

export type EvaluationState =
  | AwaitingObservation
  | AwaitingCheck
  | Complete
  | MultiCheckComplete
  | Cancelled;

export class EvaluationTransitionError extends KensaCoreError {
  constructor(message: string) {
    super("invalid_transition", message);
    this.name = "EvaluationTransitionError";
  }
}

export function parseCase(input: unknown): EvaluationCase {
  return parseInput(caseSchema, input, "case violates the core contract");
}

export function parseFailure(input: unknown): EvaluationFailure {
  return parseInput(failureSchema, input, "failure violates the core contract");
}

export function parseObservation(input: unknown): EvaluationObservation {
  return parseInput(
    observationSchema,
    input,
    "observation violates the core contract",
  );
}

export function parseCheck(input: unknown): EvaluationCheck {
  return parseInput(checkSchema, input, "check violates the core contract");
}

export function parseChecks(input: unknown): EvaluationCheck[] {
  return parseInput(
    checksSchema,
    input,
    "checks violate the core contract",
  ).sort(compareChecks);
}

export function nextAction(state: EvaluationState): EvaluationAction | null {
  switch (state.phase) {
    case "awaiting_observation":
      return "invoke_agent";
    case "awaiting_check":
      return "evaluate_check";
    case "complete":
    case "cancelled":
      return null;
  }
}

export function startCase(input: unknown): AwaitingObservation {
  return { phase: "awaiting_observation", case: parseCase(input) };
}

export function observeCase(
  state: AwaitingObservation,
  input: unknown,
): AwaitingCheck;
export function observeCase(
  state: EvaluationState,
  input: unknown,
): AwaitingCheck {
  if (state.phase !== "awaiting_observation") {
    throw new EvaluationTransitionError(
      `cannot observe case in ${state.phase} phase`,
    );
  }
  return {
    phase: "awaiting_check",
    case: state.case,
    observation: parseObservation(input),
  };
}

export function checkCase(state: AwaitingCheck, input: unknown): Complete;
export function checkCase(state: EvaluationState, input: unknown): Complete {
  if (state.phase !== "awaiting_check") {
    throw new EvaluationTransitionError(
      `cannot check case in ${state.phase} phase`,
    );
  }
  const completed = completeCase(state, [input]);
  const { checks, ...complete } = completed;
  return { ...complete, check: checks[0]! };
}

export function completeCase(
  state: AwaitingCheck,
  input: unknown,
): MultiCheckComplete;
export function completeCase(
  state: EvaluationState,
  input: unknown,
): MultiCheckComplete;
export function completeCase(
  state: EvaluationState,
  input: unknown,
): MultiCheckComplete {
  if (state.phase !== "awaiting_check") {
    throw new EvaluationTransitionError(
      `cannot complete case in ${state.phase} phase`,
    );
  }
  const checks = parseChecks(input);
  validateFailedObservation(state.observation, checks);
  const verdict = verdictForChecks(checks);
  return {
    phase: "complete",
    case: state.case,
    observation: state.observation,
    checks,
    failure: failureForVerdict(checks, verdict),
    verdict,
  };
}

export function cancelCase(
  state: AwaitingObservation | AwaitingCheck,
  input: unknown,
): Cancelled;
export function cancelCase(state: EvaluationState, input: unknown): Cancelled {
  if (state.phase === "complete" || state.phase === "cancelled") {
    throw new EvaluationTransitionError(
      `cannot cancel case in ${state.phase} phase`,
    );
  }
  const reason = parseInput(
    cancellationReasonSchema,
    input,
    "cancellation reason violates the core contract",
  );
  return {
    phase: "cancelled",
    case: state.case,
    observation: state.phase === "awaiting_check" ? state.observation : null,
    reason,
    verdict: "error",
    failure: {
      category: "harness",
      kind: "cancelled",
      message: reason,
      evidence: {},
    },
  };
}

function validateFailedObservation(
  observation: EvaluationObservation,
  checks: EvaluationCheck[],
): void {
  if (observation.failure === null) {
    return;
  }
  if (checks.length !== 1 || checks[0]!.outcome !== "error") {
    throw new EvaluationTransitionError(
      "a failed observation requires exactly one error check",
    );
  }
  if (
    canonicalJson(observation.failure) !== canonicalJson(checks[0]!.failure)
  ) {
    throw new EvaluationTransitionError(
      "observation and check failures must identify the same failure",
    );
  }
}

function verdictForChecks(checks: EvaluationCheck[]): EvaluationVerdict {
  if (checks.some((check) => check.outcome === "error")) {
    return "error";
  }
  if (checks.some((check) => check.outcome === "unsatisfied")) {
    return "fail";
  }
  if (checks.some((check) => check.outcome === "satisfied")) {
    return "pass";
  }
  return "skipped";
}

function failureForVerdict(
  checks: EvaluationCheck[],
  verdict: EvaluationVerdict,
): EvaluationFailure | null {
  const decisiveOutcome = {
    error: "error",
    fail: "unsatisfied",
    pass: null,
    skipped: "skipped",
  } as const satisfies Record<
    EvaluationVerdict,
    EvaluationCheck["outcome"] | null
  >;
  const outcome = decisiveOutcome[verdict];
  if (outcome === null) {
    return null;
  }
  return checks.find((check) => check.outcome === outcome)!.failure;
}

function compareChecks(left: EvaluationCheck, right: EvaluationCheck): number {
  return left.id < right.id ? -1 : 1;
}

function allowedOutcomes(
  category: EvaluationFailure["category"],
): readonly EvaluationCheck["outcome"][] {
  switch (category) {
    case "agent":
    case "judge":
    case "simulator":
      return ["unsatisfied", "error"];
    case "configuration":
    case "infrastructure":
    case "unknown":
      return ["error"];
    case "harness":
      return ["error", "skipped"];
  }
}

function addIssue(
  context: z.RefinementCtx,
  path: string,
  message: string,
): void {
  context.addIssue({ code: "custom", path: [path], message });
}
