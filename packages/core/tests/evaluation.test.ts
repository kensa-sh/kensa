import { describe, expect, it } from "vitest";
import type { ZodError } from "zod";

import {
  cancelCase,
  checkCase,
  completeCase,
  CoreValidationError,
  EvaluationTransitionError,
  nextAction,
  observeCase,
  parseCase,
  parseCheck,
  parseChecks,
  parseObservation,
  startCase,
  type AwaitingCheck,
  type AwaitingObservation,
  type Cancelled,
  type Complete,
  type EvaluationFailure,
  type MultiCheckComplete,
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
    expect(complete.failure).toBeNull();
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
    expect(complete.failure).toEqual(failure(category));
  });

  it.each([
    [["satisfied", "skipped"], "pass"],
    [["skipped", "skipped"], "skipped"],
    [["satisfied", "unsatisfied"], "fail"],
    [["unsatisfied", "error"], "error"],
    [["satisfied", "skipped", "unsatisfied", "error"], "error"],
  ] as const)(
    "derives a %s multi-check collection as %s regardless of order",
    (outcomes, verdict) => {
      const checks = outcomes.map((outcome, index) => ({
        id: `check-${outcomes.length - index}`,
        outcome,
        failure:
          outcome === "satisfied"
            ? null
            : failure(
                outcome === "unsatisfied"
                  ? "agent"
                  : outcome === "skipped"
                    ? "harness"
                    : "infrastructure",
              ),
      }));
      const observed = observeCase(startCase(testCase), observation());
      const forward = completeCase(observed, checks);
      const reverse = completeCase(observed, [...checks].reverse());

      expect(forward.verdict).toBe(verdict);
      expect(reverse).toEqual(forward);
      expect(forward.checks.map((check) => check.id)).toEqual(
        checks.map((check) => check.id).sort(),
      );
    },
  );

  it("selects the first canonical failure for the decisive outcome", () => {
    const result = completeCase(
      observeCase(startCase(testCase), observation()),
      [
        {
          id: "z-error",
          outcome: "error",
          failure: { ...failure("infrastructure"), kind: "z" },
        },
        {
          id: "a-fail",
          outcome: "unsatisfied",
          failure: failure("agent"),
        },
        {
          id: "a-error",
          outcome: "error",
          failure: { ...failure("infrastructure"), kind: "a" },
        },
      ],
    );

    expect(result.verdict).toBe("error");
    expect(result.failure?.kind).toBe("a");
  });

  it("does not promote skipped provenance in a passing collection", () => {
    const result = completeCase(
      observeCase(startCase(testCase), observation()),
      [
        { id: "pass", outcome: "satisfied", failure: null },
        { id: "skip", outcome: "skipped", failure: failure("harness") },
      ],
    );

    expect(result).toMatchObject({ verdict: "pass", failure: null });
    expect(result.checks[1]?.failure).toEqual(failure("harness"));
  });

  it("validates and canonicalizes multi-check collections", () => {
    expect(
      parseChecks([
        { id: " z ", outcome: "satisfied", failure: null },
        { id: "a", outcome: "satisfied", failure: null },
      ]).map((check) => check.id),
    ).toEqual(["a", "z"]);
    expect(() => parseChecks([])).toThrow(CoreValidationError);
    expect(() =>
      parseChecks([
        { id: "same", outcome: "satisfied", failure: null },
        { id: " same ", outcome: "satisfied", failure: null },
      ]),
    ).toThrow(CoreValidationError);
    expect(() =>
      parseChecks([{ id: "bad", outcome: "error", failure: null }]),
    ).toThrow(CoreValidationError);
  });

  it("requires failed observations to complete with one identical error", () => {
    const agentFailure = failure("agent");
    const observed = observeCase(
      startCase(testCase),
      observation(agentFailure),
    );
    const matching = {
      id: "agent",
      outcome: "error" as const,
      failure: agentFailure,
    };

    expect(completeCase(observed, [matching])).toMatchObject({
      verdict: "error",
      failure: agentFailure,
    });
    expect(() => completeCase(observed, [])).toThrow(CoreValidationError);
    expect(() =>
      completeCase(observed, [
        matching,
        { id: "second", outcome: "satisfied", failure: null },
      ]),
    ).toThrow(EvaluationTransitionError);
    expect(() =>
      completeCase(observed, [
        { ...matching, outcome: "unsatisfied" as const },
      ]),
    ).toThrow(EvaluationTransitionError);
    expect(() =>
      completeCase(observed, [
        {
          ...matching,
          failure: { ...agentFailure, kind: "different" },
        },
      ]),
    ).toThrow(EvaluationTransitionError);
  });

  it("keeps the single-check transition as a projection of multi-check rules", () => {
    const observed = observeCase(startCase(testCase), observation());
    const check = { id: "only", outcome: "satisfied" as const, failure: null };
    const single = checkCase(observed, check);
    const multiple = completeCase(observed, [check]);
    const { checks, ...shared } = multiple;

    expect(single).toEqual({ ...shared, check: checks[0] });
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
    const complete = checkCase(observed, {
      id: "pytest",
      outcome: "error",
      failure: agentFailure,
    });
    expect(complete.verdict).toBe("error");
    expect(complete.failure).toEqual(agentFailure);
  });

  it("rejects contradictory observation and check failures", () => {
    const observed = observeCase(
      startCase(testCase),
      observation(failure("agent")),
    );
    expect(() =>
      checkCase(observed, {
        id: "pytest",
        outcome: "error",
        failure: { ...failure("agent"), kind: "different" },
      }),
    ).toThrow(EvaluationTransitionError);
  });

  it("rejects invalid transitions", () => {
    const started = startCase(testCase);
    expect(() => checkCase(started as unknown as AwaitingCheck, {})).toThrow(
      "cannot check case in awaiting_observation phase",
    );
    const observed = observeCase(started, observation());
    expect(() =>
      observeCase(observed as unknown as AwaitingObservation, {}),
    ).toThrow(EvaluationTransitionError);
    expect(() => completeCase(started as unknown as AwaitingCheck, [])).toThrow(
      EvaluationTransitionError,
    );
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
      observation: null,
      reason: "pytest stopped",
      verdict: "error",
      failure: { category: "harness", kind: "cancelled" },
    });
    expect(nextAction(cancelled)).toBeNull();
    expect(() =>
      cancelCase(cancelled as unknown as AwaitingObservation, "again"),
    ).toThrow(EvaluationTransitionError);
    expect(() => cancelCase(started, null)).toThrow(CoreValidationError);
    expect(() => cancelCase(started, "   ")).toThrow(CoreValidationError);
    const complete = checkCase(observeCase(started, observation()), {
      id: "pytest",
      outcome: "satisfied",
      failure: null,
    });
    expect(() =>
      cancelCase(complete as unknown as AwaitingObservation, "late"),
    ).toThrow(EvaluationTransitionError);
  });

  it("preserves collected evidence when an evaluation is cancelled", () => {
    const observed = observeCase(startCase(testCase), observation());
    const cancelled = cancelCase(observed, "interrupted");

    expect(cancelled.observation).toEqual(observed.observation);
  });

  it("exports phase-specific state contracts", () => {
    const started: AwaitingObservation = startCase(testCase);
    const observed: AwaitingCheck = observeCase(started, observation());
    const complete: Complete = checkCase(observed, {
      id: "pytest",
      outcome: "satisfied",
      failure: null,
    });
    const multiComplete: MultiCheckComplete = completeCase(observed, [
      { id: "pytest", outcome: "satisfied", failure: null },
    ]);
    const cancelled: Cancelled = cancelCase(started, "stopped");

    expect([complete.phase, multiComplete.phase, cancelled.phase]).toEqual([
      "complete",
      "complete",
      "cancelled",
    ]);
  });

  it("preserves prototype-shaped metadata and failure evidence", async () => {
    const metadata = JSON.parse(
      '{"__proto__":{"safe":true},"constructor":1}',
    ) as Record<string, unknown>;
    const evidence = JSON.parse(
      '{"__proto__":{"proof":true},"constructor":2}',
    ) as Record<string, unknown>;
    const parsedCase = parseCase({ ...testCase, metadata });
    const parsedCheck = parseCheck({
      id: "pytest",
      outcome: "error",
      failure: { ...failure("infrastructure"), evidence },
    });

    expect(Object.hasOwn(parsedCase.metadata, "__proto__")).toBe(true);
    expect(Object.getPrototypeOf(parsedCase.metadata)).toBe(Object.prototype);
    expect(
      Object.hasOwn(parsedCheck.failure?.evidence ?? {}, "__proto__"),
    ).toBe(true);
    expect(Object.getPrototypeOf(parsedCheck.failure?.evidence)).toBe(
      Object.prototype,
    );
    await expect(
      import("../src/index.js").then(({ digestJson }) =>
        digestJson({ evidence: parsedCheck.failure?.evidence, metadata }),
      ),
    ).resolves.toHaveLength(64);
  });

  it.each([
    ["case.id", parseCase, { ...testCase }, "id"],
    ["case.input", parseCase, { ...testCase }, "input"],
    ["case.metadata", parseCase, { ...testCase }, "metadata"],
    ["observation.output", parseObservation, observation(), "output"],
    [
      "observation.output_recorded",
      parseObservation,
      observation(),
      "output_recorded",
    ],
    ["observation.trace", parseObservation, observation(), "trace"],
    ["observation.failure", parseObservation, observation(), "failure"],
    ["trace.spans", parseObservation, observation(), "trace.spans"],
    ["trace.agent_runs", parseObservation, observation(), "trace.agent_runs"],
    ["trace.tools", parseObservation, observation(), "trace.tools"],
    ["trace.tool_calls", parseObservation, observation(), "trace.tool_calls"],
    ["trace.incomplete", parseObservation, observation(), "trace.incomplete"],
    [
      "trace.incomplete_reason",
      parseObservation,
      observation(),
      "trace.incomplete_reason",
    ],
    ["trace.duration_ms", parseObservation, observation(), "trace.duration_ms"],
    ["trace.cost_usd", parseObservation, observation(), "trace.cost_usd"],
    [
      "trace.known_cost_usd",
      parseObservation,
      observation(),
      "trace.known_cost_usd",
    ],
    [
      "trace.cost_available",
      parseObservation,
      observation(),
      "trace.cost_available",
    ],
    ["trace.llm_turns", parseObservation, observation(), "trace.llm_turns"],
    [
      "failure.category",
      parseObservation,
      observation(failure("agent")),
      "failure.category",
    ],
    [
      "failure.kind",
      parseObservation,
      observation(failure("agent")),
      "failure.kind",
    ],
    [
      "failure.message",
      parseObservation,
      observation(failure("agent")),
      "failure.message",
    ],
    [
      "failure.evidence",
      parseObservation,
      observation(failure("agent")),
      "failure.evidence",
    ],
    [
      "check.id",
      parseCheck,
      { id: "x", outcome: "satisfied", failure: null },
      "id",
    ],
    [
      "check.outcome",
      parseCheck,
      { id: "x", outcome: "satisfied", failure: null },
      "outcome",
    ],
    [
      "check.failure",
      parseCheck,
      { id: "x", outcome: "satisfied", failure: null },
      "failure",
    ],
  ] as const)(
    "rejects missing and undefined %s",
    (_name, parse, value, path) => {
      const missing = structuredClone(value) as Record<string, unknown>;
      const undefinedValue = structuredClone(value) as Record<string, unknown>;
      const segments = path.split(".");
      const parentFor = (
        target: Record<string, unknown>,
      ): Record<string, unknown> => {
        let parent = target;
        for (const segment of segments.slice(0, -1)) {
          parent = parent[segment] as Record<string, unknown>;
        }
        return parent;
      };
      delete parentFor(missing)[segments.at(-1)!];
      parentFor(undefinedValue)[segments.at(-1)!] = undefined;

      expect(() => parse(missing)).toThrow(CoreValidationError);
      expect(() => parse(undefinedValue)).toThrow(CoreValidationError);
    },
  );

  it("accepts explicit null output when it was recorded", () => {
    expect(
      parseObservation({
        ...observation(),
        output: null,
        output_recorded: true,
      }).output,
    ).toBeNull();
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
      expect((error as CoreValidationError).issues[0]).toMatchObject({
        code: "unknown_field",
        path: [],
      });
    }
  });

  it.each([
    ["custom", "constraint"],
    ["invalid_element", "element"],
    ["invalid_format", "format"],
    ["invalid_key", "key"],
    ["too_big", "maximum"],
    ["too_small", "minimum"],
    ["not_multiple_of", "multiple"],
    ["invalid_type", "type"],
    ["invalid_union", "union"],
    ["unrecognized_keys", "unknown_field"],
    ["invalid_value", "value"],
  ] as const)("maps %s to the stable %s issue code", (source, expected) => {
    const error = new CoreValidationError("invalid", {
      issues: [{ code: source, message: "invalid", path: ["items", 1] }],
    } as ZodError);

    expect(error.issues).toEqual([
      { code: expected, message: "invalid", path: ["items", 1] },
    ]);
  });

  it("fails closed when validation produces an unknown issue code", () => {
    expect(
      () =>
        new CoreValidationError("invalid", {
          issues: [{ code: "future_code", message: "invalid", path: [] }],
        } as unknown as ZodError),
    ).toThrow(
      expect.objectContaining({
        code: "invalid_input",
        message: "validation produced an unsupported issue code",
      }),
    );
  });
});
