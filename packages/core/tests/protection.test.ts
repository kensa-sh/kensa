import { describe, expect, it } from "vitest";

import {
  bootstrapProtectionSuite,
  buildRedactedEvidenceRecord,
  CoreValidationError,
  verifyProtectionSuite,
  type ProtectionSuite,
} from "../src/index.js";

const proof = {
  version: "kensa.redactor.v2",
  mandatory: true,
  language: "en",
  value_redaction_applied: true,
  redaction_available: true,
  redacted_span_count: 0,
  changed_value_count: 0,
  secret_keys_redacted: false,
  trace_count: 1,
  ruleset_hash:
    "96332f6e9bdb07b0d837e733c393f3e4dd2ecd0b8e910fff06ba6266114ae422",
  pseudonymization: "instance-counter",
  entity_instance_counts: {},
  detectors: {},
  model: {
    name: "en_core_web_sm",
    version: "3.8.0",
    checksum_verified: true,
  },
} as const;

const bindings = {
  eval: {
    framework: "pytest",
    path: "tests/evals/test_refunds.py",
    entrypoint: "test_refund_requires_order",
  },
  workflow: {
    provider: "github-actions",
    path: ".github/workflows/kensa.yml",
    job: "kensa",
  },
  application: {
    name: "support-agent",
    environment: "staging",
  },
} as const;

function trace(id: string): Record<string, unknown> {
  return {
    schema_version: "kensa.trace_view.v2",
    id,
    name: "support",
    source: {
      provider: "local",
      import_run_id: "import",
      imported_at: "2026-08-11T00:00:00Z",
    },
    started_at_unix_nano: null,
    ended_at_unix_nano: null,
    duration_ms: 0,
    status: "unknown",
    input: null,
    output: null,
    spans: [],
  };
}

function idea(
  id: string,
  status: "pending" | "approved" | "rejected" = "approved",
): Record<string, unknown> {
  return {
    id,
    trace_ids: [`trace-${id}`],
    source: "local traces",
    status,
    failure_pattern: `${id} failed`,
    expected_outcome: `${id} succeeds`,
    expected_current_behavior: "fail",
  };
}

function draft(candidateId: string): Record<string, unknown> {
  return {
    candidate_id: candidateId,
    input: { request: candidateId },
    criteria: [
      {
        id: "judge-quality",
        description: "The response explains the required next action",
        kind: "judge",
      },
      {
        id: "assert-safe",
        description: "No state change occurs without an order",
        kind: "assertion",
      },
    ],
  };
}

async function records(...ids: string[]) {
  return Promise.all(
    ids.map((id) => buildRedactedEvidenceRecord(trace(`trace-${id}`), proof)),
  );
}

async function suite(
  overrides: Record<string, unknown> = {},
): Promise<ProtectionSuite> {
  return bootstrapProtectionSuite({
    id: "refund-protection",
    name: " Refund protection ",
    queue: {
      schema_version: "kensa.inspect.v1",
      items: [idea("refund"), idea("pending", "pending")],
    },
    evidence: await records("refund"),
    cases: [draft("refund")],
    bindings,
    ...overrides,
  });
}

describe("repository protection suites", () => {
  it("bootstraps only approved ideas with explicit repository criteria", async () => {
    const built = await suite();

    expect(built.schema_version).toBe("kensa.protection.v1");
    expect(built.name).toBe("Refund protection");
    expect(built.cases).toHaveLength(1);
    expect(built.cases[0]!.id).toBe("refund");
    expect(built.cases[0]!.criteria.map((item) => item.id)).toEqual([
      "assert-safe",
      "judge-quality",
    ]);
    expect(built.cases[0]!.source.candidate_digest).toMatch(/^[0-9a-f]{64}$/);
    expect(built.cases[0]!.source.evidence[0]!.record_digest).toMatch(
      /^[0-9a-f]{64}$/,
    );
    expect(built.digest).toMatch(/^[0-9a-f]{64}$/);
    expect(await verifyProtectionSuite(built)).toEqual(built);
  });

  it("is deterministic across queue, evidence, case, criteria, and source order", async () => {
    const queue = {
      schema_version: "kensa.inspect.v1",
      items: [idea("second"), idea("first")],
    };
    const evidence = await records("second", "first");
    const first = await suite({
      queue,
      evidence,
      cases: [draft("second"), draft("first")],
    });
    const second = await suite({
      queue: { ...queue, items: [...queue.items].reverse() },
      evidence: [...evidence].reverse(),
      cases: [draft("first"), draft("second")].map((item) => ({
        ...item,
        criteria: [...(item.criteria as unknown[])].reverse(),
      })),
    });

    expect(first).toEqual(second);
    expect(first.cases.map((item) => item.id)).toEqual(["first", "second"]);
    expect(await verifyProtectionSuite(first)).toEqual(first);
  });

  it("rejects absent approval and incomplete approval coverage", async () => {
    await expect(
      suite({
        queue: {
          schema_version: "kensa.inspect.v1",
          items: [idea("refund", "pending")],
        },
        evidence: await records("refund"),
      }),
    ).rejects.toThrow("at least one approved");
    await expect(
      suite({
        queue: {
          schema_version: "kensa.inspect.v1",
          items: [idea("refund"), idea("second")],
        },
        evidence: await records("refund", "second"),
      }),
    ).rejects.toThrow("missing approved cases: second");
    await expect(suite({ cases: [draft("unapproved")] })).rejects.toThrow(
      "unapproved cases: unapproved; missing approved cases: refund",
    );
    await expect(
      suite({ cases: [draft("refund"), draft("unapproved")] }),
    ).rejects.toThrow("unapproved cases: unapproved");
    await expect(
      suite({ cases: [draft("refund"), draft("refund")] }),
    ).rejects.toThrow("duplicate case refund");
  });

  it.each([
    ["absolute eval path", { ...bindings.eval, path: "/tests/evals/test.py" }],
    ["parent eval path", { ...bindings.eval, path: "tests/../test.py" }],
    ["empty path segment", { ...bindings.eval, path: "tests//test.py" }],
    ["Windows path", { ...bindings.eval, path: "tests\\test.py" }],
    ["NUL path", { ...bindings.eval, path: "tests/evals/test_\0.py" }],
    ["non-eval path", { ...bindings.eval, path: "tests/test_refunds.py" }],
  ])("rejects %s", async (_label, evalBinding) => {
    await expect(
      suite({ bindings: { ...bindings, eval: evalBinding } }),
    ).rejects.toThrow(CoreValidationError);
  });

  it("rejects non-root workflows and blank repository criteria", async () => {
    await expect(
      suite({
        bindings: {
          ...bindings,
          workflow: { ...bindings.workflow, path: "ci/kensa.yml" },
        },
      }),
    ).rejects.toThrow(CoreValidationError);
    await expect(
      suite({
        cases: [
          {
            ...draft("refund"),
            criteria: [{ id: "blank", description: " ", kind: "judge" }],
          },
        ],
      }),
    ).rejects.toThrow(CoreValidationError);
  });

  it("rejects duplicate or contradictory canonical suite content", async () => {
    const built = await suite();
    await expect(
      verifyProtectionSuite({
        ...built,
        cases: [built.cases[0], built.cases[0]],
      }),
    ).rejects.toThrow("duplicate case");
    await expect(
      verifyProtectionSuite({
        ...built,
        cases: [
          {
            ...built.cases[0],
            source: {
              ...built.cases[0]!.source,
              candidate_id: "different",
            },
          },
        ],
      }),
    ).rejects.toThrow("contradicts");
    await expect(
      verifyProtectionSuite({
        ...built,
        cases: [
          {
            ...built.cases[0],
            criteria: [
              built.cases[0]!.criteria[0],
              built.cases[0]!.criteria[0],
            ],
          },
        ],
      }),
    ).rejects.toThrow("criteria contains duplicate");
    await expect(
      verifyProtectionSuite({
        ...built,
        cases: [
          {
            ...built.cases[0],
            source: {
              ...built.cases[0]!.source,
              evidence: [
                built.cases[0]!.source.evidence[0],
                built.cases[0]!.source.evidence[0],
              ],
            },
          },
        ],
      }),
    ).rejects.toThrow("evidence contains duplicate");
    await expect(
      verifyProtectionSuite({ ...built, digest: "0".repeat(64) }),
    ).rejects.toThrow("not canonical");
    await expect(
      verifyProtectionSuite({ ...built, extra: true }),
    ).rejects.toThrow(CoreValidationError);
  });
});
