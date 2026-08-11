import { describe, expect, it } from "vitest";
import {
  canonicalProtocolJson,
  parseCheckResult,
  parseEvalRun,
  parseInvocation,
  parseProtocolDocument,
  parseProtocolJson,
  parseSpan,
  ProtocolError,
} from "../src/index.js";
import {
  canonicalJson,
  compareScalar,
} from "../src/protocol/canonical-json.js";
import { jsonObject } from "../src/protocol/schemas.js";
import {
  check,
  failure,
  invocation,
  protocolError,
  run,
  span,
} from "./helpers.js";

describe("protocol semantics", () => {
  it.each(["pending", "running", "pass"])(
    "accepts eval run %s without failure",
    (status) => {
      expect(parseEvalRun(run({ status })).status).toBe(status);
    },
  );

  it.each(["fail", "error", "cancelled", "interrupted"])(
    "accepts eval run %s with failure",
    (status) => {
      expect(parseEvalRun(run({ status, failure: failure() })).status).toBe(
        status,
      );
    },
  );

  it.each([
    ["pass", failure()],
    ["fail", null],
  ])("rejects contradictory eval run failure ownership", (status, value) => {
    const error = protocolError(() =>
      parseEvalRun(run({ status, failure: value })),
    );
    expect(error).toMatchObject({
      boundary: "runtime",
      code: "contradictory_fields",
      path: ["failure"],
    });
  });

  it.each(["pending", "running", "pass"])(
    "accepts invocation %s without failure",
    (status) => {
      expect(parseInvocation(invocation({ status })).status).toBe(status);
    },
  );

  it.each(["fail", "error", "cancelled", "skipped", "interrupted"])(
    "accepts invocation %s with failure",
    (status) => {
      expect(
        parseInvocation(invocation({ status, failure: failure() })).status,
      ).toBe(status);
    },
  );

  it.each([
    ["complete", null],
    ["pending", "waiting"],
    ["partial", "missing spans"],
    ["unavailable", "provider rejected the request"],
  ])("accepts evidence completeness %s", (status, reason) => {
    expect(
      parseInvocation(invocation({ evidence_completeness: { status, reason } }))
        .evidence_completeness.status,
    ).toBe(status);
  });

  it.each([
    ["complete", "unexpected"],
    ["pending", null],
    ["partial", null],
    ["unavailable", null],
  ])("rejects contradictory completeness %s", (status, reason) => {
    const error = protocolError(() =>
      parseInvocation(
        invocation({ evidence_completeness: { status, reason } }),
      ),
    );
    expect(error).toMatchObject({
      code: "contradictory_fields",
      path: ["evidence_completeness", "reason"],
    });
  });

  it("distinguishes missing output from a recorded JSON null", () => {
    expect(
      parseInvocation(invocation({ output_recorded: false, output: null }))
        .output_recorded,
    ).toBe(false);
    expect(
      parseInvocation(invocation({ output_recorded: true, output: null }))
        .output_recorded,
    ).toBe(true);
    const error = protocolError(() =>
      parseInvocation(
        invocation({ output_recorded: false, output: { answer: 42 } }),
      ),
    );
    expect(error).toMatchObject({
      code: "contradictory_fields",
      path: ["output"],
    });
  });

  it.each([
    ["unset", null],
    ["ok", null],
    ["error", "target failed"],
  ])(
    "accepts span status %s with matching message",
    (status, statusMessage) => {
      expect(
        parseSpan(span({ status, status_message: statusMessage })).status,
      ).toBe(status);
    },
  );

  it.each([
    ["error", null],
    ["ok", "unexpected"],
  ])("rejects span status %s with contradictory message", (status, value) => {
    const error = protocolError(() =>
      parseSpan(span({ status, status_message: value })),
    );
    expect(error).toMatchObject({
      code: "contradictory_fields",
      path: ["status_message"],
    });
  });

  it.each(["input", "output"] as const)(
    "rejects an unrecorded span %s value",
    (field) => {
      const error = protocolError(() =>
        parseSpan(
          span({ [`${field}_recorded`]: false, [field]: { value: true } }),
        ),
      );
      expect(error).toMatchObject({
        code: "contradictory_fields",
        path: [field],
      });
    },
  );

  it.each(["input", "output"] as const)(
    "accepts a recorded span %s",
    (field) => {
      expect(
        parseSpan(span({ [`${field}_recorded`]: true, [field]: null }))[field],
      ).toBeNull();
    },
  );

  it("parses all four document kinds through the union", () => {
    expect(parseProtocolDocument(run()).document_kind).toBe("eval_run");
    expect(parseProtocolDocument(invocation()).document_kind).toBe(
      "invocation",
    );
    expect(parseProtocolDocument(span()).document_kind).toBe("span");
    expect(parseProtocolDocument(check()).document_kind).toBe("check_result");
  });

  it.each(["pass"])("accepts check %s without failure", (status) => {
    expect(parseCheckResult(check({ status })).status).toBe(status);
  });

  it.each(["fail", "error", "skipped"])(
    "accepts check %s with failure",
    (status) => {
      expect(
        parseCheckResult(check({ status, failure: failure("judge") })).status,
      ).toBe(status);
    },
  );

  it.each([
    ["pass", failure()],
    ["fail", null],
  ])("rejects contradictory check failure ownership", (status, value) => {
    const error = protocolError(() =>
      parseCheckResult(check({ status, failure: value })),
    );
    expect(error).toMatchObject({
      code: "contradictory_fields",
      path: ["failure"],
    });
  });

  it.each([
    [run({ extra: true }), "unknown_field", ["extra"]],
    [
      run({ schema_version: "kensa.protocol.v2" }),
      "invalid_literal",
      ["schema_version"],
    ],
    [run({ document_kind: "run" }), "invalid_literal", ["document_kind"]],
    [run({ id: "run_bad" }), "invalid_identifier", ["id"]],
    [
      run({ created_at: "2023-02-29T00:00:00.000Z" }),
      "invalid_timestamp",
      ["created_at"],
    ],
    [run({ duration_ms: -1 }), "unsafe_integer", ["duration_ms"]],
    [run({ status: "successful" }), "invalid_literal", ["status"]],
    [invocation({ attempt: 0 }), "unsafe_integer", ["attempt"]],
    [
      invocation({
        case: { id: "case", input: { secret: undefined }, metadata: {} },
      }),
      "invalid_json_value",
      ["case", "input", "secret"],
    ],
  ] as const)("reports stable errors", (value, code, path) => {
    const error = protocolError(() => parseProtocolDocument(value));
    expect(error).toMatchObject({ boundary: "runtime", code, path });
    expect(error.message).not.toContain("secret");
  });

  it("owns JSON syntax and UTF-8 errors", () => {
    for (const input of ["{", new Uint8Array([0xff])]) {
      const error = protocolError(() => parseProtocolJson(input));
      expect(error).toMatchObject({
        boundary: "syntax",
        code: "invalid_json",
        path: [],
        message: "Invalid JSON",
      });
    }
    expect(
      parseProtocolJson(new TextEncoder().encode(JSON.stringify(run())))
        .document_kind,
    ).toBe("eval_run");
    const runtime = protocolError(() =>
      parseProtocolJson(JSON.stringify(run({ extra: true }))),
    );
    expect(runtime).toMatchObject({
      boundary: "runtime",
      code: "unknown_field",
    });
  });

  it("rejects unsupported JavaScript values before Zod recursion", () => {
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    const error = protocolError(() =>
      parseEvalRun(run({ attributes: { evidence: cyclic } })),
    );
    expect(error).toMatchObject({
      code: "invalid_json_value",
      path: ["attributes", "evidence", "self"],
    });
  });

  it("revalidates mutated documents before canonical serialization", () => {
    const parsed = parseEvalRun(run());
    Reflect.set(parsed, "status", "fail");
    expect(() => canonicalProtocolJson(parsed)).toThrow(ProtocolError);
  });

  it("canonicalizes by Unicode scalar value without mutating input", () => {
    const value = run({
      attributes: { "\uE000": 1, "𐀀": 2, z: [2, 1], a: true },
    });
    const before = structuredClone(value);
    const canonical = canonicalProtocolJson(value);
    expect(canonical.indexOf('"a"')).toBeLessThan(canonical.indexOf('"z"'));
    expect(canonical.indexOf('"\uE000"')).toBeLessThan(
      canonical.indexOf('"𐀀"'),
    );
    expect(canonical.endsWith("\n")).toBe(true);
    expect(canonical.endsWith("\n\n")).toBe(false);
    expect(value).toEqual(before);
  });

  it("rejects non-JSON values at internal artifact boundaries", () => {
    expect(() => canonicalJson(undefined)).toThrow(TypeError);
    expect(() => jsonObject([])).toThrow(TypeError);
  });

  it("compares Unicode keys by scalar value and prefix length", () => {
    expect(compareScalar("a", "aa")).toBeLessThan(0);
    expect(compareScalar("aa", "a")).toBeGreaterThan(0);
    expect(compareScalar("same", "same")).toBe(0);
    expect(compareScalar("\uE000", "𐀀")).toBeLessThan(0);
  });
});
