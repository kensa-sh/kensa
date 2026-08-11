import { readFileSync } from "node:fs";

import { afterEach, describe, expect, it, vi } from "vitest";

import { KensaEngine, PROTOCOL_VERSION, responseSchema } from "../src/index.js";

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

function message(id: string, request: Record<string, unknown>): string {
  return JSON.stringify({ id, request });
}

const responses = JSON.parse(
  readFileSync(new URL("fixtures/responses.json", import.meta.url), "utf8"),
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
        check: { id: "pytest", status: "pass", failure: null },
      }),
    );
    expect(result).toEqual({ id: "4", ok: true, response: responses.complete });
    expect(
      engine.processLine(
        message("5", {
          type: "check",
          evaluation_id: "eval-1",
          check: { id: "pytest", status: "pass", failure: null },
        }),
      ),
    ).toMatchObject({ ok: false, failure: { code: "unknown_evaluation" } });
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
      const engine = new KensaEngine();
      ready(engine);
      vi.spyOn(Map.prototype, "has").mockImplementationOnce(() => {
        throw thrown;
      });

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
});
