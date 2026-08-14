import { describe, expect, it, vi, type TestOptions } from "vitest";

import { createKensaTest } from "../src/vitest.js";

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

describe("Vitest adapter", () => {
  it("registers a passing evaluation with Vitest 4 argument order", async () => {
    let handler: (() => Promise<void>) | undefined;
    const register = vi.fn(
      (
        _name: string,
        _options: TestOptions,
        registered: () => Promise<void>,
      ) => {
        handler = registered;
      },
    );
    const assertEqual = vi.fn();
    const verify = vi.fn();
    const kensaTest = createKensaTest({ register, assertEqual });
    const vitestOptions = { timeout: 500 };

    kensaTest(
      "answers",
      {
        case: { id: "case", input: "hello", metadata: {} },
        observe: () => ({
          output: "hi",
          output_recorded: true,
          trace,
          failure: null,
        }),
        check: () => ({ id: "correct", outcome: "satisfied", failure: null }),
      },
      { vitest: vitestOptions, verify },
    );

    expect(register).toHaveBeenCalledWith(
      "answers",
      vitestOptions,
      expect.any(Function),
    );
    await handler!();
    expect(assertEqual).toHaveBeenCalledWith("pass", "pass");
    expect(verify).toHaveBeenCalledWith(
      expect.objectContaining({ verdict: "pass" }),
    );
  });

  it("supports an expected non-passing verdict and default options", async () => {
    let handler: (() => Promise<void>) | undefined;
    const register = (
      _name: string,
      _options: TestOptions,
      registered: () => Promise<void>,
    ): void => {
      handler = registered;
    };
    const assertEqual = vi.fn();
    const kensaTest = createKensaTest({ register, assertEqual });

    kensaTest(
      "fails as expected",
      {
        case: { id: "case", input: null, metadata: {} },
        observe: () => ({
          output: null,
          output_recorded: true,
          trace,
          failure: null,
        }),
        check: () => ({
          id: "correct",
          outcome: "unsatisfied",
          failure: {
            category: "judge",
            kind: "mismatch",
            message: "wrong",
            evidence: {},
          },
        }),
      },
      { expectedVerdict: "fail" },
    );

    await handler!();
    expect(assertEqual).toHaveBeenCalledWith("fail", "fail");
  });
});
