import { z } from "zod";

import { KensaCoreError, parseInput } from "./errors.js";
import { jsonValueSchema, type JsonValue } from "./json.js";

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
  evidence: z.record(z.string(), jsonValueSchema),
});

const caseSchema = z.strictObject({
  id: z.string().trim().min(1),
  input: jsonValueSchema,
  metadata: z.record(z.string(), jsonValueSchema),
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

const cancellationReasonSchema = z.string().trim().min(1);

export type EvaluationCase = z.infer<typeof caseSchema>;
export type EvaluationFailure = z.infer<typeof failureSchema>;
export type EvaluationObservation = z.infer<typeof observationSchema>;
export type EvaluationCheck = z.infer<typeof checkSchema>;
export type EvaluationVerdict = "pass" | "fail" | "error" | "skipped";
export type EvaluationAction = "invoke_agent" | "evaluate_check";

interface AwaitingObservation {
  phase: "awaiting_observation";
  case: EvaluationCase;
}

interface AwaitingCheck {
  phase: "awaiting_check";
  case: EvaluationCase;
  observation: EvaluationObservation;
}

interface Complete {
  phase: "complete";
  case: EvaluationCase;
  observation: EvaluationObservation;
  check: EvaluationCheck;
  verdict: EvaluationVerdict;
}

interface Cancelled {
  phase: "cancelled";
  case: EvaluationCase;
  reason: string;
  failure: EvaluationFailure;
  verdict: "error";
}

export type EvaluationState =
  AwaitingObservation | AwaitingCheck | Complete | Cancelled;

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

export function checkCase(state: EvaluationState, input: unknown): Complete {
  if (state.phase !== "awaiting_check") {
    throw new EvaluationTransitionError(
      `cannot check case in ${state.phase} phase`,
    );
  }
  const check = parseCheck(input);
  if (state.observation.failure !== null && check.outcome !== "error") {
    throw new EvaluationTransitionError(
      "a failed observation requires an error check outcome",
    );
  }
  return {
    phase: "complete",
    case: state.case,
    observation: state.observation,
    check,
    verdict: verdictFor(check.outcome),
  };
}

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

function verdictFor(outcome: EvaluationCheck["outcome"]): EvaluationVerdict {
  switch (outcome) {
    case "satisfied":
      return "pass";
    case "unsatisfied":
      return "fail";
    case "error":
      return "error";
    case "skipped":
      return "skipped";
  }
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
