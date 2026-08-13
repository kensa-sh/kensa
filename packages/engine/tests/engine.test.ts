import { readFileSync } from "node:fs";

import { afterEach, describe, expect, it, vi } from "vitest";
import { ZodError } from "zod";
import { KensaCoreError } from "@kensa/core";

import {
  ENGINE_VERSION,
  KensaEngine,
  PROTOCOL_VERSION,
  responseEnvelopeSchema,
  responseSchema,
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

const agentFailure = {
  category: "agent",
  kind: "exception",
  message: "agent raised",
  evidence: { type: "RuntimeError" },
} as const;

const requiredJudge = {
  id: "judge-1",
  criteria: "The answer is grounded.",
  required: true,
  passed: false,
  reasoning: "The answer is unsupported.",
  evidence: ["No supporting tool call."],
  provider: "openai",
  model: "judge-model",
  metadata: {},
  error: false,
  error_kind: null,
} as const;

function message(id: string, request: Record<string, unknown>): string {
  return JSON.stringify({ id, request });
}

const responseFixtures = JSON.parse(
  readFileSync(new URL("fixtures/responses.json", import.meta.url), "utf8"),
) as Record<string, unknown>;
const responses: Record<string, unknown> = {
  ...responseFixtures,
  handshake: {
    ...(responseFixtures.handshake as Record<string, unknown>),
    engine_version: ENGINE_VERSION,
  },
};
const traceView = JSON.parse(
  readFileSync(
    new URL("../../core/conformance/trace-view.json", import.meta.url),
    "utf8",
  ),
) as Record<string, unknown>;

function ready(engine: KensaEngine): void {
  const handshake = engine.processLine(
    message("1", {
      type: "handshake",
      protocol_version: PROTOCOL_VERSION,
      client: "test",
    }),
  );
  expect(handshake).toMatchObject({
    ok: true,
    response: { type: "handshake" },
  });
}

describe("KensaEngine", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("pins every success response to the shared golden contract", () => {
    for (const response of Object.values(responses)) {
      expect(responseSchema.parse(response)).toEqual(response);
    }
  });

  it("reports the engine package version in its handshake", () => {
    const packageMetadata = JSON.parse(
      readFileSync(new URL("../package.json", import.meta.url), "utf8"),
    ) as { version: string };

    expect(ENGINE_VERSION).toBe(packageMetadata.version);
    expect(responses.handshake).toMatchObject({
      engine_version: packageMetadata.version,
    });
  });

  it("rejects responses that violate core evidence contracts", () => {
    expect(() =>
      responseSchema.parse({
        ...(responses.complete as Record<string, unknown>),
        evaluation: {
          ...((responses.complete as Record<string, unknown>)
            .evaluation as Record<string, unknown>),
          output: undefined,
        },
      }),
    ).toThrow();
    expect(() =>
      responseSchema.parse({
        type: "conversation_action",
        conversation_id: "conversation",
        action: { source: "agent" },
      }),
    ).toThrow();
    expect(() =>
      responseSchema.parse({
        type: "conversation_result",
        conversation_id: "conversation",
        result: {
          phase: "complete",
          messages: [],
          output: null,
          output_recorded: false,
          termination: { source: "engine", reason: "" },
        },
      }),
    ).toThrow();
    expect(() =>
      responseSchema.parse({ type: "run_result", result: null }),
    ).toThrow(ZodError);
    expect(() =>
      responseSchema.parse({
        type: "trace_views",
        traces: [{ ...traceView, duration_ms: 99 }],
      }),
    ).toThrow();
    expect(() =>
      responseSchema.parse({
        ...(responses.cancelled as Record<string, unknown>),
        evaluation: {
          ...((responses.cancelled as Record<string, unknown>)
            .evaluation as Record<string, unknown>),
          failure: undefined,
        },
      }),
    ).toThrow();
    expect(() =>
      responseSchema.parse({
        ...(responses.run_result as Record<string, unknown>),
        result: {
          ...((responses.run_result as Record<string, unknown>)
            .result as Record<string, unknown>),
          aggregates: [{ verdict: "invented" }],
        },
      }),
    ).toThrow();
    expect(() =>
      responseSchema.parse({
        ...(responses.cancelled as Record<string, unknown>),
        evaluation: {
          ...((responses.cancelled as Record<string, unknown>)
            .evaluation as Record<string, unknown>),
          failure: { category: "harness" },
        },
      }),
    ).toThrow();
  });

  it("runs one evaluation to a core-owned verdict and releases terminal state", () => {
    const engine = new KensaEngine();
    const handshake = engine.processLine(
      message("1", {
        type: "handshake",
        protocol_version: PROTOCOL_VERSION,
        client: "test",
      }),
    );
    expect(handshake).toEqual({
      id: "1",
      ok: true,
      response: responses.handshake,
    });
    const start = engine.processLine(
      message("2", {
        type: "start_case",
        evaluation_id: "eval-1",
        case: { id: "case-1", input: "hello", metadata: {} },
      }),
    );
    expect(start).toEqual({
      id: "2",
      ok: true,
      response: responses.invoke_agent,
    });
    const observation = engine.processLine(
      message("3", {
        type: "observe",
        evaluation_id: "eval-1",
        observation: {
          output: "world",
          output_recorded: true,
          trace,
          failure: null,
        },
      }),
    );
    expect(observation).toEqual({
      id: "3",
      ok: true,
      response: responses.evaluate_check,
    });
    const result = engine.processLine(
      message("4", {
        type: "check",
        evaluation_id: "eval-1",
        checks: [{ id: "pytest", outcome: "satisfied", failure: null }],
        judges: [],
      }),
    );
    expect(result).toEqual({ id: "4", ok: true, response: responses.complete });
    expect(
      engine.processLine(
        message("5", {
          type: "check",
          evaluation_id: "eval-1",
          checks: [{ id: "pytest", outcome: "satisfied", failure: null }],
          judges: [],
        }),
      ),
    ).toMatchObject({ ok: false, failure: { code: "unknown_evaluation" } });
  });

  it("drives a direct conversation to a core-owned terminal result", () => {
    const engine = new KensaEngine();
    ready(engine);

    expect(
      engine.processLine(
        message("2", {
          type: "start_conversation",
          conversation_id: "conversation-1",
          conversation: {
            messages: [],
            mode: "direct",
            max_agent_responses: null,
            starts_with: "agent",
          },
        }),
      ),
    ).toEqual({
      id: "2",
      ok: true,
      response: responses.conversation_action,
    });

    expect(
      engine.processLine(
        message("3", {
          type: "observe_conversation",
          conversation_id: "conversation-1",
          observation: {
            source: "agent",
            content: "hello",
            output: null,
            output_recorded: false,
            termination_reason: null,
          },
        }),
      ),
    ).toEqual({
      id: "3",
      ok: true,
      response: responses.conversation_result,
    });
    expect(
      engine.processLine(
        message("4", {
          type: "observe_conversation",
          conversation_id: "conversation-1",
          observation: {
            source: "agent",
            content: "again",
            output: null,
            output_recorded: false,
            termination_reason: null,
          },
        }),
      ),
    ).toMatchObject({
      ok: false,
      failure: { code: "unknown_conversation" },
    });
  });

  it("drives simulated conversations without committing invalid turns", () => {
    const engine = new KensaEngine();
    ready(engine);
    const start = {
      type: "start_conversation",
      conversation_id: "simulated",
      conversation: {
        messages: [{ role: "system", content: "private" }],
        mode: "simulated",
        max_agent_responses: 1,
        starts_with: "simulator",
      },
    };

    expect(engine.processLine(message("2", start))).toMatchObject({
      ok: true,
      response: {
        type: "conversation_action",
        action: {
          source: "simulator",
          messages: [],
          response_index: 1,
          agent_responses: 0,
        },
      },
    });
    expect(engine.processLine(message("duplicate", start))).toMatchObject({
      ok: false,
      failure: { code: "invalid_transition" },
    });

    const wrongSource = {
      type: "observe_conversation",
      conversation_id: "simulated",
      observation: {
        source: "agent",
        content: "wrong",
        output: null,
        output_recorded: false,
        termination_reason: null,
      },
    };
    expect(
      engine.processLine(message("wrong-source", wrongSource)),
    ).toMatchObject({
      ok: false,
      failure: { code: "invalid_transition" },
    });

    const simulatorTurn = {
      type: "observe_conversation",
      conversation_id: "simulated",
      observation: {
        source: "simulator",
        content: "question",
        output: null,
        output_recorded: false,
        termination_reason: null,
      },
    };
    expect(engine.processLine(message("3", simulatorTurn))).toMatchObject({
      ok: true,
      response: {
        type: "conversation_action",
        action: {
          source: "agent",
          messages: [
            { role: "system", content: "private" },
            { role: "user", content: "question" },
          ],
          response_index: 2,
          agent_responses: 0,
        },
      },
    });
    expect(
      engine.processLine(message("replayed-simulator-turn", simulatorTurn)),
    ).toMatchObject({
      ok: false,
      failure: { code: "invalid_transition" },
    });
    expect(
      engine.processLine(
        message("4", {
          type: "observe_conversation",
          conversation_id: "simulated",
          observation: {
            source: "agent",
            content: "answer",
            output: { resolved: true },
            output_recorded: true,
            termination_reason: null,
          },
        }),
      ),
    ).toMatchObject({
      ok: true,
      response: {
        type: "conversation_result",
        result: {
          output: { resolved: true },
          output_recorded: true,
          termination: { source: "engine", reason: "max_turns" },
        },
      },
    });
  });

  it("supports cancellation", () => {
    const engine = new KensaEngine();
    ready(engine);
    engine.processLine(
      message("2", {
        type: "start_case",
        evaluation_id: "eval-1",
        case: { id: "case-1", input: null, metadata: {} },
      }),
    );
    expect(
      engine.processLine(
        message("3", {
          type: "cancel",
          evaluation_id: "eval-1",
          reason: "stopped",
        }),
      ),
    ).toMatchObject({
      ok: true,
      response: responses.cancelled,
    });
    expect(
      engine.processLine(
        message("4", {
          type: "cancel",
          evaluation_id: "eval-1",
          reason: "again",
        }),
      ),
    ).toMatchObject({ ok: false, failure: { code: "unknown_evaluation" } });
  });

  it("derives required judge checks while retaining advisory provenance", () => {
    const engine = new KensaEngine();
    ready(engine);
    engine.processLine(
      message("2", {
        type: "start_case",
        evaluation_id: "judged",
        case: { id: "case-1", input: null, metadata: {} },
      }),
    );
    engine.processLine(
      message("3", {
        type: "observe",
        evaluation_id: "judged",
        observation: {
          output: "world",
          output_recorded: true,
          trace,
          failure: null,
        },
      }),
    );

    const invalid = engine.processLine(
      message("4", {
        type: "check",
        evaluation_id: "judged",
        checks: [{ id: "pytest", outcome: "satisfied", failure: null }],
        judges: [{ ...requiredJudge, error: true }],
      }),
    );
    expect(invalid).toMatchObject({
      ok: false,
      failure: { code: "invalid_message" },
    });

    const result = engine.processLine(
      message("5", {
        type: "check",
        evaluation_id: "judged",
        checks: [{ id: "pytest", outcome: "satisfied", failure: null }],
        judges: [
          requiredJudge,
          {
            ...requiredJudge,
            id: "judge-2",
            required: false,
            criteria: "The tone is concise.",
          },
        ],
      }),
    );

    expect(result).toMatchObject({
      ok: true,
      response: {
        type: "result",
        evaluation: {
          verdict: "fail",
          checks: [
            { id: "judge-1", outcome: "unsatisfied" },
            { id: "pytest", outcome: "satisfied" },
          ],
          judges: [
            { id: "judge-1", required: true },
            { id: "judge-2", required: false },
          ],
        },
      },
    });
  });

  it("preserves failed observations through terminal error handling", () => {
    const engine = new KensaEngine();
    ready(engine);
    engine.processLine(
      message("2", {
        type: "start_case",
        evaluation_id: "failed-agent",
        case: { id: "case-1", input: null, metadata: {} },
      }),
    );
    expect(
      engine.processLine(
        message("3", {
          type: "observe",
          evaluation_id: "failed-agent",
          observation: {
            output: null,
            output_recorded: false,
            trace: {
              ...trace,
              incomplete: true,
              incomplete_reason: "agent raised",
            },
            failure: agentFailure,
          },
        }),
      ),
    ).toMatchObject({
      ok: true,
      response: { type: "action", action: "evaluate_check" },
    });
    expect(
      engine.processLine(
        message("4", {
          type: "check",
          evaluation_id: "failed-agent",
          checks: [{ id: "pytest", outcome: "error", failure: agentFailure }],
          judges: [],
        }),
      ),
    ).toMatchObject({
      ok: true,
      response: {
        type: "result",
        evaluation: { verdict: "error", failure: agentFailure },
      },
    });
    expect(
      engine.processLine(
        message("5", {
          type: "cancel",
          evaluation_id: "failed-agent",
          reason: "late",
        }),
      ),
    ).toMatchObject({ ok: false, failure: { code: "unknown_evaluation" } });
  });

  it("retains observed evidence in cancellation results", () => {
    const engine = new KensaEngine();
    ready(engine);
    engine.processLine(
      message("2", {
        type: "start_case",
        evaluation_id: "observed",
        case: { id: "case-1", input: null, metadata: {} },
      }),
    );
    engine.processLine(
      message("3", {
        type: "observe",
        evaluation_id: "observed",
        observation: {
          output: "partial",
          output_recorded: true,
          trace,
          failure: null,
        },
      }),
    );

    expect(
      engine.processLine(
        message("4", {
          type: "cancel",
          evaluation_id: "observed",
          reason: "interrupted",
        }),
      ),
    ).toMatchObject({
      ok: true,
      response: {
        evaluation: {
          phase: "cancelled",
          observation: { output: "partial", trace },
        },
      },
    });
  });

  it("builds a run result through core-owned aggregation", () => {
    const engine = new KensaEngine();
    ready(engine);

    expect(
      engine.processLine(
        message("run", {
          type: "build_run",
          run_id: "run-1",
          complete: true,
          interruption: null,
          trials: [],
        }),
      ),
    ).toEqual({ id: "run", ok: true, response: responses.run_result });
  });

  it("normalizes portable trace evidence through the core", () => {
    const engine = new KensaEngine();
    ready(engine);

    expect(
      engine.processLine(
        message("traces", { type: "normalize_traces", traces: [] }),
      ),
    ).toEqual({ id: "traces", ok: true, response: responses.trace_views });
    expect(
      engine.processLine(
        message("invalid-traces", {
          type: "normalize_traces",
          traces: [{ schema_version: "unknown" }],
        }),
      ),
    ).toMatchObject({
      id: "invalid-traces",
      ok: false,
      failure: { code: "invalid_message" },
    });
  });

  it("fails closed on malformed, unversioned, and repeated requests", () => {
    const engine = new KensaEngine();
    expect(engine.processLine("{")).toMatchObject({
      id: null,
      ok: false,
      failure: { code: "invalid_message" },
    });
    expect(
      engine.processLine(message("1", { type: "start_case" })),
    ).toMatchObject({
      ok: false,
      failure: { code: "invalid_message" },
    });
    expect(
      engine.processLine(
        message("before-handshake", {
          type: "start_case",
          evaluation_id: "early",
          case: { id: "case-1", input: null, metadata: {} },
        }),
      ),
    ).toMatchObject({ ok: false, failure: { code: "invalid_transition" } });
    expect(
      engine.processLine(
        message("2", {
          type: "handshake",
          protocol_version: "future",
          client: "test",
        }),
      ),
    ).toMatchObject({ ok: false, failure: { code: "version_mismatch" } });
    ready(engine);
    expect(
      engine.processLine(
        message("invalid-case", {
          type: "start_case",
          evaluation_id: "invalid",
          case: { id: "", input: null, metadata: {} },
        }),
      ),
    ).toMatchObject({
      ok: false,
      failure: {
        code: "invalid_message",
        details: { issues: expect.any(Array) },
      },
    });
    expect(
      engine.processLine(
        message("3", {
          type: "handshake",
          protocol_version: PROTOCOL_VERSION,
          client: "test",
        }),
      ),
    ).toMatchObject({ ok: false, failure: { code: "invalid_transition" } });
  });

  it("reports unknown evaluations and duplicate case starts", () => {
    const engine = new KensaEngine();
    ready(engine);
    expect(
      engine.processLine(
        message("2", {
          type: "observe",
          evaluation_id: "missing",
          observation: {
            output: null,
            output_recorded: false,
            trace,
            failure: null,
          },
        }),
      ),
    ).toMatchObject({ ok: false, failure: { code: "unknown_evaluation" } });
    const start = {
      type: "start_case",
      evaluation_id: "duplicate",
      case: { id: "case-1", input: null, metadata: {} },
    };
    engine.processLine(message("3", start));
    expect(engine.processLine(message("4", start))).toMatchObject({
      ok: false,
      failure: { code: "invalid_transition" },
    });
    expect(
      engine.processLine(
        message("5", {
          type: "check",
          evaluation_id: "duplicate",
          checks: [{ id: "pytest", outcome: "satisfied", failure: null }],
          judges: [],
        }),
      ),
    ).toMatchObject({ ok: false, failure: { code: "invalid_transition" } });
    engine.processLine(
      message("6", {
        type: "observe",
        evaluation_id: "duplicate",
        observation: {
          output: null,
          output_recorded: true,
          trace,
          failure: null,
        },
      }),
    );
    expect(
      engine.processLine(
        message("7", {
          type: "observe",
          evaluation_id: "duplicate",
          observation: {
            output: null,
            output_recorded: true,
            trace,
            failure: null,
          },
        }),
      ),
    ).toMatchObject({ ok: false, failure: { code: "invalid_transition" } });
  });

  it("resets all abandoned evaluation state", () => {
    const engine = new KensaEngine();
    ready(engine);
    for (const evaluationId of ["first", "second"]) {
      engine.processLine(
        message(evaluationId, {
          type: "start_case",
          evaluation_id: evaluationId,
          case: { id: evaluationId, input: null, metadata: {} },
        }),
      );
    }

    expect(engine.processLine(message("reset", { type: "reset" }))).toEqual({
      id: "reset",
      ok: true,
      response: { type: "reset", released: 2 },
    });
    expect(
      engine.processLine(
        message("missing", {
          type: "cancel",
          evaluation_id: "first",
          reason: "late",
        }),
      ),
    ).toMatchObject({ ok: false, failure: { code: "unknown_evaluation" } });
    expect(engine.reset()).toBe(0);
  });

  it("isolates interleaved conversations and resets remaining state", () => {
    const engine = new KensaEngine();
    ready(engine);
    engine.processLine(
      message("evaluation", {
        type: "start_case",
        evaluation_id: "evaluation",
        case: { id: "case", input: null, metadata: {} },
      }),
    );
    engine.processLine(
      message("first", {
        type: "start_conversation",
        conversation_id: "first",
        conversation: {
          messages: [],
          mode: "direct",
          max_agent_responses: null,
          starts_with: "agent",
        },
      }),
    );
    engine.processLine(
      message("second", {
        type: "start_conversation",
        conversation_id: "second",
        conversation: {
          messages: [],
          mode: "simulated",
          max_agent_responses: 2,
          starts_with: "simulator",
        },
      }),
    );

    expect(
      engine.processLine(
        message("second-turn", {
          type: "observe_conversation",
          conversation_id: "second",
          observation: {
            source: "simulator",
            content: "question",
            output: null,
            output_recorded: false,
            termination_reason: null,
          },
        }),
      ),
    ).toMatchObject({
      ok: true,
      response: {
        type: "conversation_action",
        conversation_id: "second",
        action: { source: "agent", response_index: 2 },
      },
    });
    expect(
      engine.processLine(
        message("finish-first", {
          type: "observe_conversation",
          conversation_id: "first",
          observation: {
            source: "agent",
            content: "done",
            output: null,
            output_recorded: false,
            termination_reason: null,
          },
        }),
      ),
    ).toMatchObject({
      ok: true,
      response: {
        type: "conversation_result",
        conversation_id: "first",
      },
    });
    expect(
      engine.processLine(
        message("released-first", {
          type: "observe_conversation",
          conversation_id: "first",
          observation: {
            source: "agent",
            content: "late",
            output: null,
            output_recorded: false,
            termination_reason: null,
          },
        }),
      ),
    ).toMatchObject({
      ok: false,
      failure: { code: "unknown_conversation" },
    });

    expect(engine.reset()).toBe(2);
  });

  it("does not commit state until the outbound response validates", () => {
    let validation = 0;
    const engine = new KensaEngine({
      nextAction: (state) =>
        state.phase === "awaiting_observation"
          ? "invoke_agent"
          : "evaluate_check",
      validateResponse: (value) => {
        validation += 1;
        if (validation === 3) {
          throw new ZodError([
            {
              code: "custom",
              message: "forced failure",
              path: ["response", 1],
            },
          ]);
        }
        return responseSchema.parse(value);
      },
    });
    ready(engine);
    engine.processLine(
      message("2", {
        type: "start_case",
        evaluation_id: "atomic",
        case: { id: "case-1", input: null, metadata: {} },
      }),
    );
    const observation = {
      type: "observe",
      evaluation_id: "atomic",
      observation: {
        output: "world",
        output_recorded: true,
        trace,
        failure: null,
      },
    };

    expect(engine.processLine(message("3", observation))).toMatchObject({
      ok: false,
      failure: {
        code: "internal",
        message: "engine produced an invalid response",
      },
    });
    expect(engine.processLine(message("4", observation))).toMatchObject({
      ok: true,
      response: { type: "action", action: "evaluate_check" },
    });
  });

  it("does not commit conversation state until its action validates", () => {
    let rejectNext = false;
    const engine = new KensaEngine({
      validateResponse: (value) => {
        if (rejectNext) {
          rejectNext = false;
          throw new ZodError([
            {
              code: "custom",
              message: "forced failure",
              path: ["conversation_action"],
            },
          ]);
        }
        return responseSchema.parse(value);
      },
    });
    ready(engine);
    engine.processLine(
      message("2", {
        type: "start_conversation",
        conversation_id: "atomic-conversation",
        conversation: {
          messages: [],
          mode: "simulated",
          max_agent_responses: 1,
          starts_with: "simulator",
        },
      }),
    );
    const observation = {
      type: "observe_conversation",
      conversation_id: "atomic-conversation",
      observation: {
        source: "simulator",
        content: "hello",
        output: null,
        output_recorded: false,
        termination_reason: null,
      },
    };

    expect(
      engine.processLine(
        message("invalid", {
          ...observation,
          observation: { ...observation.observation, content: " " },
        }),
      ),
    ).toMatchObject({
      ok: false,
      failure: { code: "invalid_message" },
    });
    rejectNext = true;
    expect(engine.processLine(message("3", observation))).toMatchObject({
      ok: false,
      failure: { code: "internal" },
    });
    expect(engine.processLine(message("4", observation))).toMatchObject({
      ok: true,
      response: {
        type: "conversation_action",
        action: { source: "agent", response_index: 2 },
      },
    });
  });

  it("fails safely when core returns no action for active state", () => {
    const engine = new KensaEngine({
      nextAction: () => null,
      validateResponse: (value) => responseSchema.parse(value),
    });
    ready(engine);

    expect(
      engine.processLine(
        message("2", {
          type: "start_case",
          evaluation_id: "no-action",
          case: { id: "case-1", input: null, metadata: {} },
        }),
      ),
    ).toMatchObject({
      ok: false,
      failure: {
        code: "internal",
        message: "active evaluation case-1 has no next action",
      },
    });
  });

  it("fails safely when core returns no conversation action", () => {
    const engine = new KensaEngine({ conversationAction: () => null });
    ready(engine);

    expect(
      engine.processLine(
        message("2", {
          type: "start_conversation",
          conversation_id: "no-action",
          conversation: {
            messages: [],
            mode: "direct",
            max_agent_responses: null,
            starts_with: "agent",
          },
        }),
      ),
    ).toMatchObject({
      ok: false,
      failure: {
        code: "internal",
        message: "active conversation has no next action",
      },
    });
  });

  it("retains request identifiers when validating invalid envelopes", () => {
    const engine = new KensaEngine();
    expect(engine.processLine("null")).toMatchObject({ id: null, ok: false });
    expect(engine.processLine(JSON.stringify({}))).toMatchObject({
      id: null,
      ok: false,
    });
    expect(engine.processLine(JSON.stringify({ id: 1 }))).toMatchObject({
      id: null,
      ok: false,
    });
    expect(engine.processLine(JSON.stringify({ id: "kept" }))).toMatchObject({
      id: "kept",
      ok: false,
    });
  });

  it.each([
    [new Error("unexpected error"), "unexpected error"],
    ["unexpected value", "unknown engine error"],
  ])(
    "returns structured internal failures for unexpected exceptions",
    (thrown, messageText) => {
      let explode = false;
      const engine = new KensaEngine({
        nextAction: (state) =>
          state.phase === "awaiting_observation"
            ? "invoke_agent"
            : "evaluate_check",
        validateResponse: (value) => {
          if (explode) {
            throw thrown;
          }
          return responseSchema.parse(value);
        },
      });
      ready(engine);
      explode = true;

      expect(
        engine.processLine(
          message("internal", {
            type: "start_case",
            evaluation_id: "eval-internal",
            case: { id: "case-1", input: null, metadata: {} },
          }),
        ),
      ).toMatchObject({
        id: "internal",
        ok: false,
        failure: { code: "internal", message: messageText },
      });
    },
  );

  it("validates every emitted failure envelope", () => {
    const preHandshake = new KensaEngine();
    const version = new KensaEngine();
    const readyEngine = new KensaEngine();
    ready(readyEngine);
    const internal = new KensaEngine({
      nextAction: () => {
        throw new Error("internal seam");
      },
      validateResponse: (value) => responseSchema.parse(value),
    });
    ready(internal);
    const failures = [
      preHandshake.processLine("{"),
      preHandshake.processLine(
        message("transition", {
          type: "start_case",
          evaluation_id: "early",
          case: { id: "case", input: null, metadata: {} },
        }),
      ),
      version.processLine(
        message("version", {
          type: "handshake",
          protocol_version: "future",
          client: "test",
        }),
      ),
      readyEngine.processLine(
        message("unknown", {
          type: "cancel",
          evaluation_id: "missing",
          reason: "stopped",
        }),
      ),
      internal.processLine(
        message("internal", {
          type: "start_case",
          evaluation_id: "internal",
          case: { id: "case", input: null, metadata: {} },
        }),
      ),
    ];

    for (const envelope of failures) {
      expect(responseEnvelopeSchema.parse(envelope)).toEqual(envelope);
    }
    expect(
      failures.map((envelope) =>
        envelope.ok ? "success" : envelope.failure.code,
      ),
    ).toEqual([
      "invalid_message",
      "invalid_transition",
      "version_mismatch",
      "unknown_evaluation",
      "internal",
    ]);
  });

  it("does not hide unexpected errors from core response validation", () => {
    const failure = new Proxy(
      {},
      {
        getPrototypeOf: () => {
          throw new Error("unexpected proxy failure");
        },
      },
    );

    expect(() =>
      responseSchema.parse({
        ...(responses.cancelled as Record<string, unknown>),
        evaluation: {
          ...((responses.cancelled as Record<string, unknown>)
            .evaluation as Record<string, unknown>),
          failure,
        },
      }),
    ).toThrow("unexpected proxy failure");
  });

  it("classifies unexpected core failures as internal", () => {
    let explode = false;
    const engine = new KensaEngine({
      nextAction: (state) =>
        state.phase === "awaiting_observation"
          ? "invoke_agent"
          : "evaluate_check",
      validateResponse: (value) => {
        if (explode) {
          throw new KensaCoreError(
            "unsupported_platform",
            "platform unavailable",
          );
        }
        return responseSchema.parse(value);
      },
    });
    ready(engine);
    explode = true;

    expect(
      engine.processLine(message("reset", { type: "reset" })),
    ).toMatchObject({
      ok: false,
      failure: { code: "internal", message: "platform unavailable" },
    });
  });
});
