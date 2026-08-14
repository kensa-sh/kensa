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
const runtimePhaseSchema = z.enum(["setup", "call", "teardown"]);
const runtimeMessageSchema = z.string().trim().min(1);
const activeOperationSchema = z.strictObject({
  name: z.string().trim().min(1),
  kind: z.enum(["span", "tool", "llm"]),
  attributes: jsonObjectSchema,
});
const runtimeOutcomeSchema = z.discriminatedUnion("kind", [
  z.strictObject({ kind: z.literal("passed") }),
  z.strictObject({
    kind: z.literal("attributed_failure"),
    failure: failureSchema,
  }),
  z.strictObject({
    kind: z.literal("configuration_error"),
    message: runtimeMessageSchema,
    exception_type: z.string().trim().min(1),
  }),
  z.strictObject({
    kind: z.literal("engine_error"),
    message: runtimeMessageSchema,
    code: z.string().trim().min(1),
  }),
  z.strictObject({
    kind: z.literal("case_contract_error"),
    message: runtimeMessageSchema,
    exception_type: z.string().trim().min(1),
  }),
  z.strictObject({
    kind: z.literal("conversation_error"),
    message: runtimeMessageSchema,
    source: z.enum(["agent", "simulator"]),
    error_kind: z.enum(["contract", "execution"]),
    timed_out: z.boolean(),
    accepted_messages: z.number().int().nonnegative(),
    cause_type: z.string().trim().min(1).nullable(),
  }),
  z.strictObject({
    kind: z.literal("skipped"),
    message: runtimeMessageSchema,
    phase: runtimePhaseSchema,
  }),
  z.strictObject({
    kind: z.literal("xfailed"),
    message: runtimeMessageSchema,
    phase: runtimePhaseSchema,
    outcome: z.string().trim().min(1),
  }),
  z.strictObject({
    kind: z.literal("lifecycle_error"),
    message: runtimeMessageSchema,
    phase: z.enum(["setup", "teardown"]),
  }),
  z.strictObject({
    kind: z.literal("assertion_failed"),
    message: runtimeMessageSchema,
    exception_type: z.string().trim().min(1),
  }),
  z.strictObject({
    kind: z.literal("exception"),
    message: runtimeMessageSchema,
    exception_type: z.string().trim().min(1),
    timed_out: z.boolean(),
  }),
  z.strictObject({
    kind: z.literal("timeout"),
    message: runtimeMessageSchema,
    phase: runtimePhaseSchema,
    timeout_s: z.number().finite().positive(),
    active_operation: activeOperationSchema.nullable(),
  }),
]);
const runtimeClassificationSchema = z
  .strictObject({
    verdict: z.enum(["pass", "fail", "error", "skipped"]),
    failure: failureSchema.nullable(),
    check: checkSchema,
  })
  .superRefine((classification, context) => {
    const expectedOutcome = {
      pass: "satisfied",
      fail: "unsatisfied",
      error: "error",
      skipped: "skipped",
    } as const;
    if (classification.check.id !== "pytest") {
      addIssue(context, "check.id", "runtime check must use the pytest ID");
    }
    if (
      classification.check.outcome !== expectedOutcome[classification.verdict]
    ) {
      addIssue(
        context,
        "check.outcome",
        "runtime check contradicts its verdict",
      );
    }
    if (
      canonicalJson(classification.check.failure) !==
      canonicalJson(classification.failure)
    ) {
      addIssue(
        context,
        "check.failure",
        "runtime check contradicts its failure",
      );
    }
  });
const runtimeTerminalSchema = z
  .strictObject({
    verdict: z.enum(["pass", "fail", "error", "skipped"]),
    failure: failureSchema.nullable(),
  })
  .superRefine((terminal, context) => {
    const requiresFailure = terminal.verdict !== "pass";
    if (requiresFailure !== (terminal.failure !== null)) {
      addIssue(
        context,
        "failure",
        `runtime ${terminal.verdict} verdict has contradictory failure provenance`,
      );
      return;
    }
    if (terminal.failure === null) {
      return;
    }
    const expectedOutcome = {
      error: "error",
      fail: "unsatisfied",
      pass: "satisfied",
      skipped: "skipped",
    } as const;
    if (
      !allowedOutcomes(terminal.failure.category).includes(
        expectedOutcome[terminal.verdict],
      )
    ) {
      addIssue(
        context,
        "verdict",
        `${terminal.failure.category} failure cannot produce ${terminal.verdict}`,
      );
    }
  });

export type EvaluationCase = z.infer<typeof caseSchema>;
export type EvaluationFailure = z.infer<typeof failureSchema>;
export type EvaluationObservation = z.infer<typeof observationSchema>;
export type EvaluationCheck = z.infer<typeof checkSchema>;
export type EvaluationVerdict = "pass" | "fail" | "error" | "skipped";
export type EvaluationAction = "invoke_agent" | "evaluate_check";
export type RuntimeOutcome = z.infer<typeof runtimeOutcomeSchema>;
export type RuntimeTerminal = z.infer<typeof runtimeTerminalSchema>;

export interface RuntimeClassification {
  verdict: EvaluationVerdict;
  failure: EvaluationFailure | null;
  check: EvaluationCheck;
}

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

export function classifyRuntimeOutcome(input: unknown): RuntimeClassification {
  const outcome = parseInput(
    runtimeOutcomeSchema,
    input,
    "runtime outcome violates the core contract",
  );
  switch (outcome.kind) {
    case "passed":
      return runtimeClassification("pass", null);
    case "attributed_failure":
      return runtimeClassification("error", outcome.failure);
    case "configuration_error":
      return runtimeClassification("error", {
        category: "configuration",
        kind: "llm",
        message: outcome.message,
        evidence: { exception_type: outcome.exception_type },
      });
    case "engine_error":
      return runtimeClassification("error", {
        category: "infrastructure",
        kind: outcome.code,
        message: outcome.message,
        evidence: { exception_type: "KensaEngineError" },
      });
    case "case_contract_error":
      return runtimeClassification("error", {
        category: "harness",
        kind: "case_contract",
        message: outcome.message,
        evidence: { exception_type: outcome.exception_type },
      });
    case "conversation_error":
      return runtimeClassification("error", conversationFailure(outcome));
    case "skipped":
      return runtimeClassification("skipped", {
        category: "harness",
        kind: "skip",
        message: outcome.message,
        evidence: { phase: outcome.phase },
      });
    case "xfailed":
      return runtimeClassification("skipped", {
        category: "harness",
        kind: "xfail",
        message: outcome.message,
        evidence: { phase: outcome.phase, outcome: outcome.outcome },
      });
    case "lifecycle_error":
      return runtimeClassification("error", {
        category: "harness",
        kind: outcome.phase,
        message: outcome.message,
        evidence: { phase: outcome.phase },
      });
    case "assertion_failed":
      return runtimeClassification("fail", {
        category: "agent",
        kind: "assertion",
        message: outcome.message,
        evidence: { exception_type: outcome.exception_type },
      });
    case "exception":
      return runtimeClassification("error", {
        category: "unknown",
        kind: outcome.timed_out ? "timeout" : outcome.exception_type,
        message: outcome.message,
        evidence: { exception_type: outcome.exception_type },
      });
    case "timeout":
      return runtimeClassification("error", timeoutFailure(outcome));
  }
}

export function resolveRuntimeOutcome(
  currentInput: unknown,
  outcomeInput: unknown,
): RuntimeClassification {
  const current =
    currentInput === null
      ? null
      : parseInput(
          runtimeTerminalSchema,
          currentInput,
          "runtime terminal outcome violates the core contract",
        );
  const outcome = parseInput(
    runtimeOutcomeSchema,
    outcomeInput,
    "runtime outcome violates the core contract",
  );
  if (
    current !== null &&
    (current.verdict === "fail" || current.verdict === "error") &&
    (outcome.kind === "skipped" || outcome.kind === "xfailed") &&
    outcome.phase === "teardown"
  ) {
    return runtimeClassification(current.verdict, current.failure);
  }
  return classifyRuntimeOutcome(outcome);
}

export function parseRuntimeClassification(
  input: unknown,
): RuntimeClassification {
  return parseInput(
    runtimeClassificationSchema,
    input,
    "runtime classification violates the core contract",
  );
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

function runtimeClassification(
  verdict: EvaluationVerdict,
  failure: EvaluationFailure | null,
): RuntimeClassification {
  const outcome = {
    pass: "satisfied",
    fail: "unsatisfied",
    error: "error",
    skipped: "skipped",
  } as const satisfies Record<EvaluationVerdict, EvaluationCheck["outcome"]>;
  return parseRuntimeClassification({
    verdict,
    failure,
    check: { id: "pytest", outcome: outcome[verdict], failure },
  });
}

function conversationFailure(
  outcome: Extract<RuntimeOutcome, { kind: "conversation_error" }>,
): EvaluationFailure {
  const category =
    outcome.source === "simulator"
      ? "simulator"
      : outcome.error_kind === "contract"
        ? "harness"
        : "agent";
  const kind =
    outcome.error_kind === "contract"
      ? outcome.source === "agent"
        ? "agent_contract"
        : "contract"
      : outcome.timed_out
        ? "timeout"
        : "execution";
  return {
    category,
    kind,
    message: outcome.message,
    evidence: {
      source: outcome.source,
      accepted_messages: outcome.accepted_messages,
      ...(outcome.cause_type === null
        ? {}
        : { cause_type: outcome.cause_type }),
    },
  };
}

function timeoutFailure(
  outcome: Extract<RuntimeOutcome, { kind: "timeout" }>,
): EvaluationFailure {
  const operation = outcome.active_operation;
  const source =
    operation?.name === "kensa.conversation.respond"
      ? operation.attributes["kensa.conversation.source"]
      : null;
  const category =
    outcome.phase !== "call"
      ? "harness"
      : source === "simulator"
        ? "simulator"
        : operation?.name === "judge"
          ? "judge"
          : "agent";
  return {
    category,
    kind: outcome.phase === "call" ? "timeout" : outcome.phase,
    message: outcome.message,
    evidence: {
      timeout_s: outcome.timeout_s,
      phase: outcome.phase,
      ...(operation === null ? {} : { active_operation: operation }),
    },
  };
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
