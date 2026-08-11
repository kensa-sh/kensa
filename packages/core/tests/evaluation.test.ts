import { describe, expect, it } from "vitest";

import {
  cancelCase,
  checkCase,
  EvaluationTransitionError,
  observeCase,
  startCase,
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

describe("evaluation transitions", () => {
  it("derives a passing verdict after an observation and check", () => {
    const started = startCase(testCase);
    const observed = observeCase(started, {
      output: "hello",
      output_recorded: true,
      trace,
      failure: null,
    });
    const complete = checkCase(observed, {
      id: "pytest",
      status: "pass",
      failure: null,
    });

    expect(complete.verdict).toBe("pass");
    expect(complete.observation.output).toBe("hello");
  });

  it.each(["fail", "error", "skipped"] as const)(
    "derives a %s verdict",
    (status) => {
      const failure = {
        category: "agent" as const,
        kind: status,
        message: status,
        evidence: {},
      };
      const observed = observeCase(startCase(testCase), {
        output: null,
        output_recorded: false,
        trace,
        failure,
      });
      const complete = checkCase(observed, {
        id: "pytest",
        status,
        failure,
      });

      expect(complete.verdict).toBe(status);
    },
  );

  it("rejects invalid transitions and contradictory outcomes", () => {
    const started = startCase(testCase);
    expect(() => checkCase(started, {})).toThrow(EvaluationTransitionError);
    const observed = observeCase(started, {
      output: null,
      output_recorded: false,
      trace,
      failure: {
        category: "agent",
        kind: "execution",
        message: "failed",
        evidence: {},
      },
    });
    expect(() => observeCase(observed, {})).toThrow(EvaluationTransitionError);
    expect(() =>
      checkCase(observed, { id: "pytest", status: "pass", failure: null }),
    ).toThrow(EvaluationTransitionError);
    expect(() =>
      checkCase(
        observeCase(startCase(testCase), {
          output: null,
          output_recorded: false,
          trace,
          failure: null,
        }),
        {
          id: "pytest",
          status: "pass",
          failure: {
            category: "agent",
            kind: "assertion",
            message: "failed",
            evidence: {},
          },
        },
      ),
    ).toThrow();
    expect(() =>
      checkCase(
        observeCase(startCase(testCase), {
          output: null,
          output_recorded: false,
          trace,
          failure: null,
        }),
        { id: "pytest", status: "fail", failure: null },
      ),
    ).toThrow();
  });

  it("cancels only active evaluations with a reason", () => {
    const started = startCase(testCase);
    const cancelled = cancelCase(started, "pytest stopped");
    expect(cancelled).toMatchObject({
      phase: "cancelled",
      reason: "pytest stopped",
    });
    expect(() => cancelCase(cancelled, "again")).toThrow(
      EvaluationTransitionError,
    );
    expect(() => cancelCase(started, "")).toThrow(EvaluationTransitionError);
  });

  it("validates public inputs strictly", () => {
    expect(() => startCase({ ...testCase, extra: true })).toThrow();
    expect(() => startCase({ ...testCase, input: Number.NaN })).toThrow();
  });
});
