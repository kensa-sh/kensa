import { z } from "zod";

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

export const failureSchema = z.strictObject({
  category: z.enum(failureCategories),
  kind: z.string().min(1),
  message: z.string().min(1),
  evidence: z.record(z.string(), jsonValueSchema),
});

export const caseSchema = z.strictObject({
  id: z.string().min(1),
  input: jsonValueSchema,
  metadata: z.record(z.string(), jsonValueSchema),
});

export const traceSchema = z.strictObject({
  spans: z.array(jsonValueSchema),
  agent_runs: z.array(jsonValueSchema),
  tools: z.array(z.string()),
  tool_calls: z.array(jsonValueSchema),
  incomplete: z.boolean(),
  incomplete_reason: z.string().nullable(),
  duration_ms: z.number().finite().nonnegative(),
  cost_usd: z.number().finite().nonnegative().nullable(),
  known_cost_usd: z.number().finite().nonnegative().nullable(),
  cost_available: z.boolean(),
  llm_turns: z.number().int().nonnegative(),
});

export const observationSchema = z.strictObject({
  output: jsonValueSchema.nullable(),
  output_recorded: z.boolean(),
  trace: traceSchema,
  failure: failureSchema.nullable(),
});

export const checkSchema = z
  .strictObject({
    id: z.string().min(1),
    status: z.enum(["pass", "fail", "error", "skipped"]),
    failure: failureSchema.nullable(),
  })
  .superRefine((check, context) => {
    const requiresFailure = check.status !== "pass";
    if (requiresFailure !== (check.failure !== null)) {
      context.addIssue({
        code: "custom",
        path: ["failure"],
        message: `${check.status} check has contradictory failure provenance`,
      });
    }
  });

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
}

export type EvaluationState =
  AwaitingObservation | AwaitingCheck | Complete | Cancelled;

export class EvaluationTransitionError extends Error {
  readonly code = "invalid_transition";
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
  return { phase: "awaiting_observation", case: caseSchema.parse(input) };
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
    observation: observationSchema.parse(input),
  };
}

export function checkCase(state: EvaluationState, input: unknown): Complete {
  if (state.phase !== "awaiting_check") {
    throw new EvaluationTransitionError(
      `cannot check case in ${state.phase} phase`,
    );
  }
  const check = checkSchema.parse(input);
  if (state.observation.failure !== null && check.status === "pass") {
    throw new EvaluationTransitionError(
      "a failed observation cannot pass its check",
    );
  }
  return {
    phase: "complete",
    case: state.case,
    observation: state.observation,
    check,
    verdict: check.status,
  };
}

export function cancelCase(state: EvaluationState, reason: string): Cancelled {
  if (state.phase === "complete" || state.phase === "cancelled") {
    throw new EvaluationTransitionError(
      `cannot cancel case in ${state.phase} phase`,
    );
  }
  if (reason.length === 0) {
    throw new EvaluationTransitionError(
      "cancellation reason must not be empty",
    );
  }
  return { phase: "cancelled", case: state.case, reason };
}
