import { expect } from "vitest";
import { ProtocolError } from "../src/index.js";

export const RUN_ID = "run_018f2f2e-8c70-7c31-8c70-123456789abc";
export const INVOCATION_ID = "inv_018f2f2e-8c70-7c31-8c70-123456789abc";
export const CHECK_ID = "chk_018f2f2e-8c70-7c31-8c70-123456789abc";

export function run(overrides: Readonly<Record<string, unknown>> = {}) {
  return {
    schema_version: "kensa.protocol.v1",
    document_kind: "eval_run",
    id: RUN_ID,
    status: "pass",
    created_at: "2026-08-07T01:02:03.004Z",
    started_at: null,
    ended_at: null,
    duration_ms: null,
    attributes: {},
    failure: null,
    ...overrides,
  };
}

export function failure(category = "agent") {
  return {
    category,
    kind: "timeout",
    message: "Target timed out",
    evidence: {},
  };
}

export function invocation(overrides: Readonly<Record<string, unknown>> = {}) {
  return {
    schema_version: "kensa.protocol.v1",
    document_kind: "invocation",
    id: INVOCATION_ID,
    run_id: RUN_ID,
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
    ...overrides,
  };
}

export function span(overrides: Readonly<Record<string, unknown>> = {}) {
  return {
    schema_version: "kensa.protocol.v1",
    document_kind: "span",
    invocation_id: INVOCATION_ID,
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
    ...overrides,
  };
}

export function check(overrides: Readonly<Record<string, unknown>> = {}) {
  return {
    schema_version: "kensa.protocol.v1",
    document_kind: "check_result",
    id: CHECK_ID,
    invocation_id: INVOCATION_ID,
    name: "check",
    status: "pass",
    started_at: null,
    ended_at: null,
    duration_ms: null,
    evidence: {},
    failure: null,
    ...overrides,
  };
}

export function protocolError(action: () => unknown): ProtocolError {
  try {
    action();
  } catch (error) {
    expect(error).toBeInstanceOf(ProtocolError);
    return error as ProtocolError;
  }
  throw new Error("Expected ProtocolError");
}
