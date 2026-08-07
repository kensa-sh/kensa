import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import {
  canonicalProtocolJson,
  checkResultSchema,
  evalRunSchema,
  generateProtocolSchemas,
  invocationSchema,
  isJsonObject,
  isJsonValue,
  parseCheckResult,
  parseEvalRun,
  parseInvocation,
  parseProtocolDocument,
  parseProtocolJson,
  parseSpan,
  ProtocolError,
  spanSchema,
  timestampSchema,
} from "../src/index.js";

const run = {
  schema_version: "kensa.protocol.v1",
  document_kind: "eval_run",
  id: "run_018f2f2e-8c70-7c31-8c70-123456789abc",
  status: "pass",
  created_at: "2026-08-07T01:02:03.004Z",
  started_at: null,
  ended_at: null,
  duration_ms: null,
  attributes: {},
  failure: null,
};
describe("protocol", () => {
  it("parses and canonicalizes a run", () => {
    const value = { ...run, attributes: { z: [1, true, null], a: "é", aa: 1 } };
    expect(parseProtocolDocument(value).document_kind).toBe("eval_run");
    expect(parseEvalRun(value).status).toBe("pass");
    expect(canonicalProtocolJson(value)).toContain('\n  "attributes": {');
  });
  it("rejects unknown fields", () => {
    expect(() => parseProtocolDocument({ ...run, extra: true })).toThrowError(
      ProtocolError,
    );
  });
  it("rejects contradictory states", () => {
    expect(() =>
      parseProtocolDocument({ ...run, status: "fail" }),
    ).toThrowError(ProtocolError);
  });
  it("rejects invalid JSON syntax", () => {
    expect(() => parseProtocolJson("{")).toThrowError(ProtocolError);
  });
  it("rejects cycles", () => {
    const value: Record<string, unknown> = {};
    value.self = value;
    expect(() =>
      parseProtocolDocument({ ...run, attributes: value }),
    ).toThrowError();
  });
  it("covers JSON value boundaries", () => {
    const cycle: unknown[] = [];
    cycle.push(cycle);
    expect(isJsonValue(null)).toBe(true);
    expect(isJsonValue(undefined)).toBe(false);
    expect(isJsonValue(Number.NaN)).toBe(false);
    expect(isJsonValue(1n)).toBe(false);
    expect(isJsonValue([1, { a: 2 }])).toBe(true);
    expect(isJsonValue(cycle)).toBe(false);
    expect(isJsonValue(Object.create({ bad: true }))).toBe(false);
    expect(isJsonValue({ bad: undefined })).toBe(false);
    expect(isJsonObject({})).toBe(true);
    expect(isJsonObject([])).toBe(false);
  });
  it("parses each document kind", () => {
    const invocation = {
      schema_version: "kensa.protocol.v1",
      document_kind: "invocation",
      id: "inv_018f2f2e-8c70-7c31-8c70-123456789abc",
      run_id: run.id,
      case: { id: "case", input: null, metadata: {} },
      attempt: 1,
      status: "pass",
      started_at: null,
      ended_at: null,
      duration_ms: null,
      output_recorded: true,
      output: null,
      provenance: {
        producer: "test",
        producer_version: "1",
        adapter: null,
        adapter_version: null,
        runtime: "node",
        runtime_version: "24",
        revision: null,
        environment: null,
        effects: "none",
      },
      evidence_completeness: { status: "complete", reason: null },
      attributes: {},
      failure: null,
    };
    const span = {
      schema_version: "kensa.protocol.v1",
      document_kind: "span",
      invocation_id: invocation.id,
      trace_id: "0123456789abcdef0123456789abcdef",
      span_id: "0123456789abcdef",
      parent_span_id: null,
      name: "span",
      span_kind: "internal",
      status: "unset",
      status_message: null,
      started_at: null,
      ended_at: null,
      duration_ms: null,
      input_recorded: false,
      input: null,
      output_recorded: false,
      output: null,
      attributes: {},
    };
    const check = {
      schema_version: "kensa.protocol.v1",
      document_kind: "check_result",
      id: "chk_018f2f2e-8c70-7c31-8c70-123456789abc",
      invocation_id: invocation.id,
      name: "check",
      status: "pass",
      started_at: null,
      ended_at: null,
      duration_ms: null,
      evidence: {},
      failure: null,
    };
    expect(parseInvocation(invocation).attempt).toBe(1);
    expect(
      parseInvocation({
        ...invocation,
        status: "fail",
        failure: {
          category: "agent",
          kind: "error",
          message: "failed",
          evidence: {},
        },
        evidence_completeness: { status: "pending", reason: "waiting" },
      }).status,
    ).toBe("fail");
    expect(parseSpan(span).status).toBe("unset");
    expect(
      parseSpan({ ...span, status: "error", status_message: "failed" }).status,
    ).toBe("error");
    expect(parseCheckResult(check).status).toBe("pass");
    expect(
      parseCheckResult({
        ...check,
        status: "fail",
        failure: {
          category: "judge",
          kind: "mismatch",
          message: "failed",
          evidence: {},
        },
      }).status,
    ).toBe("fail");
  });
  it("rejects semantic contradictions", () => {
    expect(() =>
      invocationSchema.parse({
        schema_version: "kensa.protocol.v1",
        document_kind: "invocation",
        id: "inv_018f2f2e-8c70-7c31-8c70-123456789abc",
        run_id: run.id,
        case: { id: "case", input: null, metadata: {} },
        attempt: 1,
        status: "fail",
        started_at: null,
        ended_at: null,
        duration_ms: null,
        output_recorded: false,
        output: { value: true },
        provenance: {
          producer: "test",
          producer_version: "1",
          adapter: null,
          adapter_version: null,
          runtime: "node",
          runtime_version: "24",
          revision: null,
          environment: null,
          effects: "none",
        },
        evidence_completeness: { status: "pending", reason: null },
        attributes: {},
        failure: null,
      }),
    ).toThrow();
    expect(() =>
      spanSchema.parse({
        schema_version: "kensa.protocol.v1",
        document_kind: "span",
        invocation_id: "inv_018f2f2e-8c70-7c31-8c70-123456789abc",
        trace_id: "00000000000000000000000000000000",
        span_id: "0123456789abcdef",
        parent_span_id: null,
        name: "span",
        span_kind: "internal",
        status: "error",
        status_message: null,
        started_at: null,
        ended_at: null,
        duration_ms: null,
        input_recorded: false,
        input: null,
        output_recorded: false,
        output: null,
        attributes: {},
      }),
    ).toThrow();
    const validSpan = {
      schema_version: "kensa.protocol.v1",
      document_kind: "span",
      invocation_id: "inv_018f2f2e-8c70-7c31-8c70-123456789abc",
      trace_id: "0123456789abcdef0123456789abcdef",
      span_id: "0123456789abcdef",
      parent_span_id: null,
      name: "span",
      span_kind: "internal",
      status: "unset",
      status_message: null,
      started_at: null,
      ended_at: null,
      duration_ms: null,
      input_recorded: false,
      input: null,
      output_recorded: false,
      output: null,
      attributes: {},
    };
    expect(() =>
      spanSchema.parse({ ...validSpan, input: { value: true } }),
    ).toThrow();
    expect(() =>
      spanSchema.parse({ ...validSpan, output: { value: true } }),
    ).toThrow();
    expect(() =>
      checkResultSchema.parse({
        schema_version: "kensa.protocol.v1",
        document_kind: "check_result",
        id: "chk_018f2f2e-8c70-7c31-8c70-123456789abc",
        invocation_id: "inv_018f2f2e-8c70-7c31-8c70-123456789abc",
        name: "check",
        status: "pass",
        started_at: null,
        ended_at: null,
        duration_ms: null,
        evidence: {},
        failure: {
          category: "agent",
          kind: "bad",
          message: "bad",
          evidence: {},
        },
      }),
    ).toThrow();
  });
  it("generates all schemas", () => {
    expect(Object.keys(generateProtocolSchemas())).toEqual([
      "eval-run",
      "invocation",
      "span",
      "check-result",
      "protocol-document",
    ]);
    expect(evalRunSchema).toBeDefined();
  });
  it("reports stable primitive and syntax errors", () => {
    expect(() =>
      parseProtocolDocument({ ...run, created_at: "2026-02-29T01:02:03.004Z" }),
    ).toThrowError(/Invalid input/);
    expect(() =>
      parseProtocolDocument({ ...run, id: "run_bad" }),
    ).toThrowError();
    expect(() =>
      parseProtocolDocument({ ...run, status: "unknown" }),
    ).toThrowError();
    expect(() =>
      parseProtocolDocument({
        ...run,
        duration_ms: Number.MAX_SAFE_INTEGER + 1,
      }),
    ).toThrowError();
    expect(() =>
      parseProtocolDocument({ ...run, attributes: { bad: undefined } }),
    ).toThrowError();
    expect(
      parseProtocolJson(new TextEncoder().encode(JSON.stringify(run)))
        .document_kind,
    ).toBe("eval_run");
    for (const value of [
      "",
      "2026-02-29T01:02:03.004Z",
      "0000-01-01T01:02:03.004Z",
      "2026-13-01T01:02:03.004Z",
      "2026-01-01T24:02:03.004Z",
    ])
      expect(timestampSchema.safeParse(value).success).toBe(false);
  });
  it("runs the committed conformance manifest", () => {
    const root = new URL("../../../fixtures/conformance/v1/", import.meta.url);
    const manifest = JSON.parse(
      readFileSync(new URL("manifest.json", root), "utf8"),
    ) as {
      fixtures: Array<{
        path: string;
        valid: boolean;
        expected_code: string | null;
      }>;
    };
    for (const fixture of manifest.fixtures) {
      const value = JSON.parse(
        readFileSync(new URL(fixture.path, root), "utf8"),
      ) as unknown;
      if (fixture.valid)
        expect(() => parseProtocolDocument(value)).not.toThrow();
      else
        expect(() => parseProtocolDocument(value)).toThrowError(
          fixture.expected_code === "unknown_field"
            ? /Unrecognized/
            : undefined,
        );
    }
  });
});
