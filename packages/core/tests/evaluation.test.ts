import { describe, expect, it } from "vitest";

import {
  cancelCase,
  checkCase,
  CoreValidationError,
  EvaluationTransitionError,
  nextAction,
  observeCase,
  parseCase,
  parseCheck,
  parseObservation,
  startCase,
  type EvaluationFailure,
} from "../src/index.js";

const testCase = { id: "hello", input: "world", metadata: {} };
const trace = {
  spans: [],
  agent_runs: [],
  tools: [],
  tool_calls: [],
  incomplete: false,
  incomplete_reason: null,
  duration_ms: 0,
  cost_usd: null,
  known_cost_usd: null,
  cost_available: false,
  llm_turns: 0,
};

function failure(category: EvaluationFailure["category"]): EvaluationFailure {
  return {
    category,
    kind: "test",
    message: "test failure",
    evidence: {},
  };
}

function observation(
  agentFailure: EvaluationFailure | null = null,
): Record<string, unknown> {
  return {
    output: agentFailure === null ? "hello" : null,
    output_recorded: agentFailure === null,
    trace,
    failure: agentFailure,
  };
}

describe("evaluation transitions", () => {
  it("derives a passing verdict from a satisfied check observation", () => {
    const started = startCase(testCase);
    expect(nextAction(started)).toBe("invoke_agent");
    const observed = observeCase(started, observation());
    expect(nextAction(observed)).toBe("evaluate_check");
    const complete = checkCase(observed, {
      id: "pytest",
      outcome: "satisfied",
      failure: null,
    });

    expect(complete.verdict).toBe("pass");
    expect(complete.observation.output).toBe("hello");
    expect(nextAction(complete)).toBeNull();
  });

  it.each([
    ["unsatisfied", "fail", "agent"],
    ["error", "error", "infrastructure"],
    ["skipped", "skipped", "harness"],
  ] as const)("derives %s as a %s verdict", (outcome, verdict, category) => {
    const complete = checkCase(
      observeCase(startCase(testCase), observation()),
      { id: "pytest", outcome, failure: failure(category) },
    );

    expect(complete.verdict).toBe(verdict);
  });

  it("requires failed agent observations to produce errors", () => {
    const agentFailure = failure("agent");
    const observed = observeCase(
      startCase(testCase),
      observation(agentFailure),
    );
    expect(() =>
      checkCase(observed, {
        id: "pytest",
        outcome: "unsatisfied",
        failure: agentFailure,
      }),
    ).toThrow(EvaluationTransitionError);
    expect(
      checkCase(observed, {
        id: "pytest",
        outcome: "error",
        failure: agentFailure,
      }).verdict,
    ).toBe("error");
  });

  it("rejects invalid transitions", () => {
    const started = startCase(testCase);
    expect(() => checkCase(started, {})).toThrow(EvaluationTransitionError);
    const observed = observeCase(started, observation());
    expect(() => observeCase(observed, {})).toThrow(EvaluationTransitionError);
  });

  it.each([
    ["agent", "skipped"],
    ["judge", "skipped"],
    ["simulator", "skipped"],
    ["configuration", "unsatisfied"],
    ["infrastructure", "unsatisfied"],
    ["unknown", "unsatisfied"],
    ["harness", "unsatisfied"],
  ] as const)("rejects a %s failure with a %s outcome", (category, outcome) => {
    expect(() =>
      parseCheck({ id: "pytest", outcome, failure: failure(category) }),
    ).toThrow(CoreValidationError);
  });

  it("rejects contradictory check provenance", () => {
    expect(() =>
      parseCheck({
        id: "pytest",
        outcome: "satisfied",
        failure: failure("agent"),
      }),
    ).toThrow(CoreValidationError);
    expect(() =>
      parseCheck({ id: "pytest", outcome: "error", failure: null }),
    ).toThrow(CoreValidationError);
  });

  it.each([
    ["incomplete reason on complete trace", { incomplete_reason: "partial" }],
    ["missing incomplete reason", { incomplete: true }],
    ["cost present while unavailable", { cost_usd: 1 }],
    ["cost missing while available", { cost_available: true }],
    [
      "known cost differs from available total",
      { cost_available: true, cost_usd: 2, known_cost_usd: 1 },
    ],
  ])("rejects %s", (_name, mutation) => {
    expect(() =>
      parseObservation({ ...observation(), trace: { ...trace, ...mutation } }),
    ).toThrow(CoreValidationError);
  });

  it("accepts partial known cost when total cost is unavailable", () => {
    expect(
      parseObservation({
        ...observation(),
        trace: { ...trace, known_cost_usd: 1 },
      }).trace,
    ).toMatchObject({ cost_available: false, known_cost_usd: 1 });
  });

  it("rejects a value when output was not recorded", () => {
    expect(() =>
      parseObservation({ ...observation(), output_recorded: false }),
    ).toThrow(CoreValidationError);
  });

  it("cancels active evaluations with a validated terminal outcome", () => {
    const started = startCase(testCase);
    const cancelled = cancelCase(started, "  pytest stopped  ");
    expect(cancelled).toMatchObject({
      phase: "cancelled",
      reason: "pytest stopped",
      verdict: "error",
      failure: { category: "harness", kind: "cancelled" },
    });
    expect(nextAction(cancelled)).toBeNull();
    expect(() => cancelCase(cancelled, "again")).toThrow(
      EvaluationTransitionError,
    );
    expect(() => cancelCase(started, null)).toThrow(CoreValidationError);
    expect(() => cancelCase(started, "   ")).toThrow(CoreValidationError);
    const complete = checkCase(observeCase(started, observation()), {
      id: "pytest",
      outcome: "satisfied",
      failure: null,
    });
    expect(() => cancelCase(complete, "late")).toThrow(
      EvaluationTransitionError,
    );
  });

  it("reports stable validation errors without exposing zod", () => {
    try {
      parseCase({ ...testCase, extra: true });
      throw new Error("expected validation failure");
    } catch (error) {
      expect(error).toBeInstanceOf(CoreValidationError);
      expect(error).toMatchObject({
        name: "CoreValidationError",
        code: "invalid_input",
        message: "case violates the core contract",
      });
      expect((error as CoreValidationError).issues).toHaveLength(1);
    }
  });
});
