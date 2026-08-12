import { expect } from "vitest";

import { kensaTest } from "../src/vitest.js";

kensaTest(
  "runs an evaluation through the default Vitest adapter",
  {
    case: { id: "case", input: "hello", metadata: {} },
    observe: () => ({
      output: "hi",
      output_recorded: true,
      trace: {
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
      },
      failure: null,
    }),
    check: () => ({ id: "correct", outcome: "satisfied", failure: null }),
  },
  {
    verify: (result) => {
      expect(result.verdict).toBe("pass");
    },
  },
);
