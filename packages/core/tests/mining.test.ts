import { describe, expect, it } from "vitest";

import {
  bindInspectCandidates,
  buildRedactedEvidenceRecord,
  CoreValidationError,
  KensaCoreError,
  mineTraceBehaviors,
  parseInspectQueue,
  traceSummary,
} from "../src/index.js";

const redactionProof = {
  version: "kensa.redactor.v2",
  mandatory: true,
  language: "en",
  value_redaction_applied: true,
  redaction_available: true,
  redacted_span_count: 1,
  changed_value_count: 1,
  secret_keys_redacted: false,
  trace_count: 1,
  ruleset_hash:
    "96332f6e9bdb07b0d837e733c393f3e4dd2ecd0b8e910fff06ba6266114ae422",
  pseudonymization: "instance-counter",
  entity_instance_counts: { PERSON: 1 },
  detectors: { adapter: { version: "test" } },
  model: {
    name: "en_core_web_sm",
    version: "3.8.0",
    checksum_verified: true,
  },
} as const;

function trace(
  id: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    schema_version: "kensa.trace_view.v2",
    id,
    name: "support",
    source: {
      provider: "local",
      import_run_id: "import",
      imported_at: "2026-08-11T00:00:00Z",
    },
    started_at_unix_nano: "1000000",
    ended_at_unix_nano: "3000000",
    duration_ms: 2,
    status: "error",
    input: "refund",
    output: null,
    spans: [span(id)],
    ...overrides,
  };
}

function span(traceId: string): Record<string, unknown> {
  return {
    id: `${traceId}-span`,
    trace_id: traceId,
    parent_id: null,
    name: "issue refund",
    kind: "tool",
    tool_name: "issue_refund",
    started_at_unix_nano: "1000000",
    ended_at_unix_nano: "3000000",
    duration_ms: 2,
    status: "error",
    status_message: "missing order",
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
  };
}

function queue(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    schema_version: "kensa.inspect.v1",
    items: [
      {
        id: "refund-without-order",
        trace_ids: ["trace-a", "trace-b"],
        source: "local traces",
        failure_pattern: "refund attempted without order identity",
        expected_outcome: "request order identity before refund",
        expected_current_behavior: "fail",
      },
    ],
    ...overrides,
  };
}

async function evidence(
  ...traces: Array<Record<string, unknown>>
): Promise<unknown[]> {
  return Promise.all(
    traces.map((item) => buildRedactedEvidenceRecord(item, redactionProof)),
  );
}

describe("trace mining contracts", () => {
  it("builds the existing compact trace summary", () => {
    expect(traceSummary(trace("trace-a"))).toEqual({
      id: "trace-a",
      name: "support",
      status: "error",
      started_at_unix_nano: "1000000",
      duration_ms: 2,
      span_count: 1,
      source: { provider: "local" },
    });
  });

  it("groups equivalent behavior and separates material signatures", async () => {
    const clusters = await mineTraceBehaviors(
      await evidence(
        trace("trace-b"),
        trace("trace-a"),
        trace("trace-c", { name: "billing" }),
        trace("trace-d", { status: "ok", spans: [] }),
      ),
    );

    expect(clusters).toHaveLength(3);
    const grouped = clusters.find((cluster) => cluster.occurrences === 2)!;
    expect(grouped.trace_ids).toEqual(["trace-a", "trace-b"]);
    expect(grouped.tool_sequence).toEqual(["issue_refund"]);
    expect(grouped.error_messages).toEqual(["missing order"]);
    expect(grouped.id).toBe(`behavior_${grouped.digest.slice(0, 24)}`);
    expect(grouped.digest).toMatch(/^[0-9a-f]{64}$/);
    expect(grouped.evidence_digest).toMatch(/^[0-9a-f]{64}$/);
    expect(clusters.map((cluster) => cluster.id)).toEqual(
      [...clusters.map((cluster) => cluster.id)].sort(),
    );
    expect(await mineTraceBehaviors([])).toEqual([]);
    const sparse = await mineTraceBehaviors(
      await evidence(
        trace("trace-e", {
          spans: [
            {
              ...span("trace-e"),
              tool_name: null,
              status_message: null,
            },
          ],
        }),
        trace("trace-f", {
          spans: [
            {
              ...span("trace-f"),
              tool_name: null,
              status: "ok",
            },
          ],
        }),
      ),
    );
    expect(sparse.every((cluster) => cluster.tool_sequence.length === 0)).toBe(
      true,
    );
    expect(sparse.every((cluster) => cluster.error_messages.length === 0)).toBe(
      true,
    );
  });

  it("normalizes inspect queue defaults and whitespace", () => {
    const parsed = parseInspectQueue({
      items: [
        {
          id: " candidate ",
          trace_ids: [" trace-a "],
          source: " source ",
          failure_pattern: " pattern ",
          expected_outcome: " outcome ",
          expected_current_behavior: "pass",
          case_shape: " shape ",
          risks: " risk ",
        },
      ],
    });

    expect(parsed).toEqual({
      schema_version: "kensa.inspect.v1",
      items: [
        {
          id: "candidate",
          trace_ids: ["trace-a"],
          source: "source",
          status: "pending",
          failure_pattern: "pattern",
          expected_outcome: "outcome",
          expected_current_behavior: "pass",
          proposed_checks: [],
          case_shape: "shape",
          risks: "risk",
        },
      ],
    });
    expect(parseInspectQueue({})).toEqual({
      schema_version: "kensa.inspect.v1",
      items: [],
    });
  });

  it("binds candidates to stable evidence identities", async () => {
    const first = await bindInspectCandidates(
      queue(),
      await evidence(trace("trace-b"), trace("trace-a")),
    );
    const second = await bindInspectCandidates(
      queue(),
      await evidence(trace("trace-a"), trace("trace-b")),
    );

    expect(first).toEqual(second);
    expect(first.schema_version).toBe("kensa.candidate_set.v1");
    expect(first.digest).toMatch(/^[0-9a-f]{64}$/);
    expect(first.candidates[0]!.digest).toMatch(/^[0-9a-f]{64}$/);
    expect(first.candidates[0]!.evidence).toHaveLength(2);
    expect(first.candidates[0]!.evidence[0]!.record_digest).toMatch(
      /^[0-9a-f]{64}$/,
    );
  });

  it("sorts candidates independently of queue order", async () => {
    const base = queue().items as Array<Record<string, unknown>>;
    const other = {
      ...base[0],
      id: "another-candidate",
      trace_ids: ["trace-a"],
    };
    const bound = await bindInspectCandidates(
      queue({ items: [base[0], other] }),
      await evidence(trace("trace-a"), trace("trace-b")),
    );

    expect(bound.candidates.map((candidate) => candidate.item.id)).toEqual([
      "another-candidate",
      "refund-without-order",
    ]);
  });

  it.each([
    ["unknown fields", queue({ extra: true })],
    ["invalid ID", queue({ items: [{ id: "INVALID" }] })],
    [
      "duplicate item IDs",
      queue({
        items: [
          (queue().items as Array<unknown>)[0],
          (queue().items as Array<unknown>)[0],
        ],
      }),
    ],
    [
      "duplicate trace IDs",
      queue({
        items: [
          {
            ...(queue().items as Array<Record<string, unknown>>)[0],
            trace_ids: ["trace-a", "trace-a"],
          },
        ],
      }),
    ],
    [
      "blank optional text",
      queue({
        items: [
          {
            ...(queue().items as Array<Record<string, unknown>>)[0],
            risks: " ",
          },
        ],
      }),
    ],
    [
      "blank proposed check",
      queue({
        items: [
          {
            ...(queue().items as Array<Record<string, unknown>>)[0],
            proposed_checks: [" "],
          },
        ],
      }),
    ],
  ])("rejects %s", (_label, value) => {
    expect(() => parseInspectQueue(value)).toThrow(CoreValidationError);
  });

  it("rejects candidates whose trace evidence is unavailable", async () => {
    await expect(
      bindInspectCandidates(queue(), await evidence(trace("trace-a"))),
    ).rejects.toThrow(KensaCoreError);
  });

  it("rejects raw, tampered, and duplicate trace evidence", async () => {
    await expect(mineTraceBehaviors([trace("trace-a")])).rejects.toThrow(
      CoreValidationError,
    );
    const records = await evidence(trace("trace-a"));
    await expect(
      mineTraceBehaviors([
        { ...(records[0] as object), digest: "0".repeat(64) },
      ]),
    ).rejects.toThrow(KensaCoreError);
    await expect(mineTraceBehaviors([records[0], records[0]])).rejects.toThrow(
      KensaCoreError,
    );
  });
});
