import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  buildEvidenceRecord,
  CoreValidationError,
  KensaCoreError,
  normalizeTraceView,
  normalizeTraceViews,
  parseEvidenceRecord,
  parseTraceView,
  parseTraceViews,
  sourceIdentity,
  verifyEvidenceRecord,
  type TraceView,
} from "../src/index.js";

function trace(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    schema_version: "kensa.trace_view.v2",
    id: "trace_local",
    name: "support agent",
    source: {
      provider: "local",
      import_run_id: "import-1",
      imported_at: "2026-08-11T00:00:00Z",
    },
    started_at_unix_nano: null,
    ended_at_unix_nano: null,
    duration_ms: 999,
    status: "unknown",
    input: { request: "refund" },
    output: null,
    spans: [
      span({
        id: "second",
        parent_id: "first",
        started_at_unix_nano: "1786400000002000000",
        ended_at_unix_nano: "1786400000004000000",
        status: "error",
        status_message: "refund failed",
      }),
      span({
        id: "first",
        started_at_unix_nano: 1_000_000,
        ended_at_unix_nano: 2_000_000,
        status: "ok",
      }),
    ],
    ...overrides,
  };
}

function span(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: "span",
    trace_id: "trace_local",
    parent_id: null,
    name: "agent",
    kind: "span",
    tool_name: null,
    started_at_unix_nano: null,
    ended_at_unix_nano: null,
    duration_ms: 99,
    status: "unknown",
    status_message: null,
    input: null,
    output: null,
    usage: {
      model_provider: null,
      model: null,
      input_tokens: null,
      output_tokens: null,
      total_tokens: null,
      cache_read_input_tokens: null,
      cache_creation_input_tokens: null,
      cost_usd: null,
    },
    ...overrides,
  };
}

describe("portable trace evidence", () => {
  it("accepts the shared Python and TypeScript trace fixture", () => {
    const fixture = JSON.parse(
      readFileSync(
        new URL("../conformance/trace-view.json", import.meta.url),
        "utf8",
      ),
    ) as unknown;

    expect(normalizeTraceView(fixture)).toEqual(fixture);
  });

  it("normalizes exact timestamps, span order, duration, and derived status", () => {
    const normalized = normalizeTraceView(trace());

    expect(normalized.started_at_unix_nano).toBe("1000000");
    expect(normalized.ended_at_unix_nano).toBe("1786400000004000000");
    expect(normalized.duration_ms).toBe(1_786_400_000_003);
    expect(normalized.status).toBe("error");
    expect(normalized.spans.map((item) => item.id)).toEqual([
      "first",
      "second",
    ]);
    expect(normalized.spans.map((item) => item.duration_ms)).toEqual([1, 2]);
    expect(parseTraceView(normalized)).toEqual(normalized);
  });

  it("preserves explicit trace boundaries and statuses", () => {
    const normalized = normalizeTraceView(
      trace({
        started_at_unix_nano: "00010",
        ended_at_unix_nano: "00020",
        status: "ok",
        spans: [span()],
      }),
    );

    expect(normalized.started_at_unix_nano).toBe("10");
    expect(normalized.ended_at_unix_nano).toBe("20");
    expect(normalized.duration_ms).toBe(0.00001);
    expect(normalized.status).toBe("ok");
    expect(normalized.spans[0]!.duration_ms).toBe(0);
  });

  it("normalizes and orders trace collections", () => {
    const first = trace({ id: "b", spans: [] });
    const second = trace({ id: "a", spans: [] });

    expect(normalizeTraceViews([first, second]).map((item) => item.id)).toEqual(
      ["a", "b"],
    );
    expect(normalizeTraceViews([second, first]).map((item) => item.id)).toEqual(
      ["a", "b"],
    );
    expect(parseTraceViews([])).toEqual([]);
    expect(normalizeTraceView(trace({ spans: [] }))).toMatchObject({
      started_at_unix_nano: null,
      ended_at_unix_nano: null,
      duration_ms: 0,
      status: "unknown",
    });
  });

  it("derives stable source identities independent of import metadata", async () => {
    const first = await sourceIdentity(trace());
    const second = await sourceIdentity(
      trace({
        source: {
          provider: "local",
          import_run_id: "import-2",
          imported_at: "2026-08-12T00:00:00Z",
        },
      }),
    );

    expect(first).toEqual(second);
    expect(first.source_id).toBe(`trace_${first.digest.slice(0, 24)}`);
    expect(first.digest).toMatch(/^[0-9a-f]{64}$/);
  });

  it("builds a portable evidence record", async () => {
    const record = await buildEvidenceRecord(trace({ spans: [] }));

    expect(record.schema_version).toBe("kensa.evidence.v1");
    expect(record.identity.kind).toBe("trace");
    expect(record.trace).toEqual(normalizeTraceView(trace({ spans: [] })));
    expect(parseEvidenceRecord(record)).toEqual(record);
    expect(await verifyEvidenceRecord(record)).toEqual(record);
  });

  it("rejects malformed and incorrectly bound evidence records", async () => {
    const record = await buildEvidenceRecord(trace({ spans: [] }));
    expect(() =>
      parseEvidenceRecord({
        ...record,
        identity: { ...record.identity, source_id: "trace_invalid" },
      }),
    ).toThrow(CoreValidationError);
    await expect(
      verifyEvidenceRecord({
        ...record,
        identity: { ...record.identity, digest: "0".repeat(64) },
      }),
    ).rejects.toThrow(KensaCoreError);
  });

  it("returns canonical trace evidence after verification", async () => {
    const record = await buildEvidenceRecord(trace());
    const reversed = {
      ...record,
      trace: { ...record.trace, spans: [...record.trace.spans].reverse() },
    };

    expect((await verifyEvidenceRecord(reversed)).trace.spans).toEqual(
      record.trace.spans,
    );
  });

  it("derives ok only when every non-empty span status is ok", () => {
    expect(
      normalizeTraceView(trace({ spans: [span({ status: "ok" })] })).status,
    ).toBe("ok");
    expect(
      normalizeTraceView(
        trace({ spans: [span({ status: "ok" }), span({ id: "other" })] }),
      ).status,
    ).toBe("unknown");
  });

  it.each([
    ["extra trace fields", trace({ extra: true })],
    ["invalid timestamp", trace({ started_at_unix_nano: "soon" })],
    [
      "unsafe timestamp number",
      trace({ started_at_unix_nano: Number.MAX_SAFE_INTEGER + 1 }),
    ],
    [
      "trace time order",
      trace({
        started_at_unix_nano: "2",
        ended_at_unix_nano: "1",
        spans: [],
      }),
    ],
    [
      "span time order",
      trace({
        spans: [span({ started_at_unix_nano: "2", ended_at_unix_nano: "1" })],
      }),
    ],
    ["duplicate spans", trace({ spans: [span(), span()] })],
    ["wrong trace ID", trace({ spans: [span({ trace_id: "other" })] })],
    ["self parent", trace({ spans: [span({ parent_id: "span" })] })],
    ["orphan parent", trace({ spans: [span({ parent_id: "missing" })] })],
    [
      "cyclic ancestry",
      trace({
        spans: [
          span({ id: "first", parent_id: "second" }),
          span({ id: "second", parent_id: "first" }),
        ],
      }),
    ],
    [
      "invalid usage",
      trace({
        spans: [
          span({
            usage: {
              model_provider: null,
              model: null,
              input_tokens: -1,
              output_tokens: null,
              total_tokens: null,
              cache_read_input_tokens: null,
              cache_creation_input_tokens: null,
              cost_usd: null,
            },
          }),
        ],
      }),
    ],
  ])("rejects %s", (_label, value) => {
    expect(() => parseTraceView(value)).toThrow(CoreValidationError);
  });

  it("rejects duplicate trace IDs in a collection", () => {
    expect(() => parseTraceViews([trace(), trace()])).toThrow(
      CoreValidationError,
    );
  });

  it("sorts null timestamps after known timestamps and breaks ties by ID", () => {
    const normalized = normalizeTraceView(
      trace({
        spans: [
          span({ id: "z" }),
          span({ id: "b", started_at_unix_nano: "2" }),
          span({ id: "a", started_at_unix_nano: "2" }),
          span({ id: "first", started_at_unix_nano: "1" }),
        ],
      }),
    );

    expect(normalized.spans.map((item) => item.id)).toEqual([
      "first",
      "a",
      "b",
      "z",
    ]);
    expect(
      normalizeTraceView(
        trace({
          spans: [
            span({ id: "known", started_at_unix_nano: "1" }),
            span({ id: "unknown" }),
          ],
        }),
      ).spans.map((item) => item.id),
    ).toEqual(["known", "unknown"]);
  });

  it("retains the latest end time regardless of span start order", () => {
    const normalized = normalizeTraceView(
      trace({
        spans: [
          span({
            id: "early",
            started_at_unix_nano: "1",
            ended_at_unix_nano: "9",
          }),
          span({
            id: "late",
            started_at_unix_nano: "2",
            ended_at_unix_nano: "3",
          }),
        ],
      }),
    );

    expect(normalized.ended_at_unix_nano).toBe("9");
  });

  it("rejects derived durations outside the interoperable number range", () => {
    expect(() =>
      normalizeTraceView(
        trace({
          started_at_unix_nano: "0",
          ended_at_unix_nano: `1${"0".repeat(400)}`,
          spans: [],
        }),
      ),
    ).toThrow("duration exceeds");
  });

  it("accepts its public output type", () => {
    const value: TraceView = normalizeTraceView(trace({ spans: [] }));
    expect(value.schema_version).toBe("kensa.trace_view.v2");
  });
});
