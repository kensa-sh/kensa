import { describe, expect, it } from "vitest";

import {
  completeCaseWithJudges,
  CoreValidationError,
  observeCase,
  parseJudgeObservations,
  startCase,
} from "../src/index.js";

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

function observed(failure: Record<string, unknown> | null = null) {
  return observeCase(startCase({ id: "case", input: "hello", metadata: {} }), {
    output: "world",
    output_recorded: true,
    trace,
    failure,
  });
}

function judge(
  id: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id,
    criteria: `Criterion ${id}`,
    required: true,
    passed: true,
    reasoning: "The criterion is satisfied.",
    evidence: ["observed output"],
    provider: "openai",
    model: "judge-model",
    metadata: { temperature: 0 },
    error: false,
    error_kind: null,
    ...overrides,
  };
}

describe("judge observations", () => {
  it.each([
    [judge("positive"), { outcome: "satisfied", failure: null }],
    [
      judge("negative", {
        passed: false,
        reasoning: "The answer is unsupported.",
      }),
      {
        outcome: "unsatisfied",
        failure: {
          category: "judge",
          kind: "criteria",
          message: "The answer is unsupported.",
        },
      },
    ],
    [
      judge("execution", {
        passed: false,
        reasoning: "Judge provider timed out.",
        error: true,
        error_kind: "execution",
      }),
      {
        outcome: "error",
        failure: {
          category: "judge",
          kind: "execution",
          message: "Judge provider timed out.",
        },
      },
    ],
    [
      judge("contract", {
        passed: false,
        reasoning: "Judge result violates the contract.",
        error: true,
        error_kind: "contract",
      }),
      {
        outcome: "error",
        failure: {
          category: "judge",
          kind: "contract",
          message: "Judge result violates the contract.",
        },
      },
    ],
  ])("maps a required judge to a core check", (observation, expected) => {
    const result = completeCaseWithJudges(
      observed(),
      [{ id: "pytest", outcome: "satisfied", failure: null }],
      [observation],
    );

    expect(
      result.checks.find((check) => check.id === observation.id),
    ).toMatchObject(expected);
  });

  it("retains advisory judges without changing the verdict", () => {
    const result = completeCaseWithJudges(
      observed(),
      [{ id: "pytest", outcome: "satisfied", failure: null }],
      [
        judge("advisory", {
          required: false,
          passed: false,
          reasoning: "Directional concern.",
        }),
      ],
    );

    expect(result.verdict).toBe("pass");
    expect(result.checks.map((check) => check.id)).toEqual(["pytest"]);
    expect(result.judges[0]).toMatchObject({
      id: "advisory",
      required: false,
      reasoning: "Directional concern.",
      provider: "openai",
      model: "judge-model",
      evidence: ["observed output"],
      metadata: { temperature: 0 },
    });
  });

  it("canonicalizes judge observations and the combined checks", () => {
    const forward = completeCaseWithJudges(
      observed(),
      [{ id: "pytest", outcome: "satisfied", failure: null }],
      [judge("judge-2"), judge("judge-1")],
    );
    const reverse = completeCaseWithJudges(
      observed(),
      [{ id: "pytest", outcome: "satisfied", failure: null }],
      [judge("judge-1"), judge("judge-2")],
    );

    expect(reverse).toEqual(forward);
    expect(forward.judges.map((item) => item.id)).toEqual([
      "judge-1",
      "judge-2",
    ]);
    expect(forward.checks.map((check) => check.id)).toEqual([
      "judge-1",
      "judge-2",
      "pytest",
    ]);
  });

  it.each([
    ["duplicate IDs", [judge("same"), judge(" same ")]],
    ["reserved pytest ID", [judge("pytest")]],
    ["an error without a kind", [judge("bad", { error: true })]],
    [
      "an error kind without an error",
      [judge("bad", { error_kind: "execution" })],
    ],
    [
      "a passing error",
      [judge("bad", { error: true, error_kind: "execution" })],
    ],
  ])("rejects %s", (_label, observations) => {
    expect(() => parseJudgeObservations(observations)).toThrow(
      CoreValidationError,
    );
  });

  it("rejects collisions between pytest and required judge checks", () => {
    expect(() =>
      completeCaseWithJudges(
        observed(),
        [{ id: "judge-1", outcome: "satisfied", failure: null }],
        [judge("judge-1")],
      ),
    ).toThrow(CoreValidationError);
  });

  it("preserves a failed observation and retains judge provenance", () => {
    const failure = {
      category: "agent",
      kind: "execution",
      message: "Agent crashed.",
      evidence: {},
    };
    const result = completeCaseWithJudges(
      observed(failure),
      [{ id: "pytest", outcome: "error", failure }],
      [judge("earlier", { passed: false })],
    );

    expect(result).toMatchObject({ verdict: "error", failure });
    expect(result.checks.map((check) => check.id)).toEqual(["pytest"]);
    expect(result.judges.map((item) => item.id)).toEqual(["earlier"]);
  });
});
