import { describe, expect, it } from "vitest";

import {
  bootstrapProtectionSuite,
  buildProtectionResult,
  buildRedactedEvidenceRecord,
  buildRunResult,
  buildSyncBatch,
  buildSyncReceipt,
  CoreValidationError,
  KensaCoreError,
  previewSync,
  verifyProtectionResult,
  verifySyncBatch,
  type ProtectionSuite,
  type Trial,
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

const github = {
  repository: "kensa-sh/example",
  repository_id: "123456",
  event: "pull_request",
  sha: "a".repeat(40),
  ref: "refs/pull/42/merge",
  workflow_path: ".github/workflows/kensa.yml",
  job: "kensa",
  run_id: "987654",
  run_attempt: 1,
  actor: "octocat",
  application: "support-agent",
  environment: "staging",
} as const;

function trace(id: string): Record<string, unknown> {
  return {
    schema_version: "kensa.trace_view.v2",
    id: `trace-${id}`,
    name: null,
    source: {
      provider: "local",
      import_run_id: "import",
      imported_at: "2026-08-12T00:00:00Z",
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

async function suite(ids: string[] = ["refund"]): Promise<ProtectionSuite> {
  const evidence = await Promise.all(
    ids.map((id) => buildRedactedEvidenceRecord(trace(id), proof)),
  );
  return bootstrapProtectionSuite({
    id: "refund-protection",
    name: "Refund protection",
    queue: {
      schema_version: "kensa.inspect.v1",
      items: ids.map((id) => ({
        id,
        trace_ids: [`trace-${id}`],
        source: "local",
        status: "approved",
        failure_pattern: `${id} failed`,
        expected_outcome: `${id} succeeds`,
        expected_current_behavior: "fail",
      })),
    },
    evidence,
    cases: ids.map((id) => ({
      candidate_id: id,
      input: { request: id },
      criteria: [
        {
          id: "safe",
          description: "The protected behavior remains safe",
          kind: "assertion",
        },
      ],
    })),
    bindings: {
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
      application: { name: "support-agent", environment: "staging" },
    },
  });
}

function trial(caseId = "refund", overrides: Partial<Trial> = {}): Trial {
  return {
    nodeid: `tests/evals/test_refunds.py::test_refund_requires_order[${caseId}-trial1]`,
    group_id: `tests/evals/test_refunds.py::test_refund_requires_order[${caseId}]`,
    case_id: caseId,
    trial_index: 1,
    configured_trials: 1,
    status: "pass",
    case: { id: caseId, input: { request: caseId } },
    output: "ok",
    failure: null,
    duration_ms: 1,
    trace: {},
    judges: [],
    active_operation: null,
    smoke: false,
    ...overrides,
  };
}

function result(trials: Trial[] = [trial()], complete = true) {
  return buildRunResult({
    run_id: "kensa-run",
    complete,
    interruption: null,
    trials,
  });
}

describe("GitHub protection results", () => {
  it("builds and verifies a complete GitHub-bound result", async () => {
    const protectionSuite = await suite();
    const built = await buildProtectionResult({
      suite: protectionSuite,
      result: result(),
      github,
    });

    expect(built.schema_version).toBe("kensa.protection_result.v1");
    expect(built.suite.digest).toBe(protectionSuite.digest);
    expect(built.github.sha).toBe(github.sha);
    expect(built.digest).toMatch(/^[0-9a-f]{64}$/);
    expect(await verifyProtectionResult(built)).toEqual(built);
    const exactNode = result([
      trial("refund", {
        nodeid: "tests/evals/test_refunds.py::test_refund_requires_order",
      }),
    ]);
    await expect(
      buildProtectionResult({
        suite: protectionSuite,
        result: exactNode,
        github,
      }),
    ).resolves.toMatchObject({ result: exactNode });
    for (const casePayload of [
      { id: "refund", messages: { request: "refund" } },
      { id: "refund", payload: { request: "refund" } },
    ]) {
      await expect(
        buildProtectionResult({
          suite: protectionSuite,
          result: result([trial("refund", { case: casePayload })]),
          github,
        }),
      ).resolves.toMatchObject({ suite: protectionSuite });
    }
  });

  it.each([
    ["workflow_path", ".github/workflows/other.yml"],
    ["job", "other"],
    ["application", "other-agent"],
    ["environment", "production"],
  ])("rejects a mismatched GitHub %s binding", async (field, value) => {
    await expect(
      buildProtectionResult({
        suite: await suite(),
        result: result(),
        github: { ...github, [field]: value },
      }),
    ).rejects.toThrow(`GitHub ${field} contradicts`);
  });

  it.each([
    ["pull_request", "refs/heads/main"],
    ["push", "refs/pull/42/merge"],
    ["workflow_dispatch", "refs/pull/42/merge"],
  ])("rejects contradictory %s refs", async (event, ref) => {
    await expect(
      buildProtectionResult({
        suite: await suite(),
        result: result(),
        github: { ...github, event, ref },
      }),
    ).rejects.toThrow("event contradicts");
  });

  it.each(["push", "workflow_dispatch"] as const)(
    "accepts a branch ref for %s",
    async (event) => {
      await expect(
        buildProtectionResult({
          suite: await suite(),
          result: result(),
          github: { ...github, event, ref: "refs/heads/main" },
        }),
      ).resolves.toMatchObject({ github: { event } });
    },
  );

  it("rejects incomplete, smoke, wrongly bound, unknown, and missing trials", async () => {
    const protectionSuite = await suite();
    await expect(
      buildProtectionResult({
        suite: protectionSuite,
        result: result([], false),
        github,
      }),
    ).rejects.toThrow("complete run");
    await expect(
      buildProtectionResult({
        suite: protectionSuite,
        result: result([trial("refund", { smoke: true })]),
        github,
      }),
    ).rejects.toThrow("smoke trials");
    await expect(
      buildProtectionResult({
        suite: protectionSuite,
        result: result([trial("refund", { nodeid: "tests/other.py::test" })]),
        github,
      }),
    ).rejects.toThrow("eval binding");
    await expect(
      buildProtectionResult({
        suite: protectionSuite,
        result: result([trial("other")]),
        github,
      }),
    ).rejects.toThrow("unknown case other");
    await expect(
      buildProtectionResult({
        suite: protectionSuite,
        result: result([
          trial("refund", { case: { id: "refund", input: "different" } }),
        ]),
        github,
      }),
    ).rejects.toThrow("case input");
    await expect(
      buildProtectionResult({
        suite: protectionSuite,
        result: result([trial("refund", { case: { id: "refund" } })]),
        github,
      }),
    ).rejects.toThrow("case input");
    await expect(
      buildProtectionResult({
        suite: await suite(["first", "second"]),
        result: result([]),
        github,
      }),
    ).rejects.toThrow("missing cases: first, second");
  });

  it("rejects malformed and tampered attestations", async () => {
    const protectionSuite = await suite();
    const built = await buildProtectionResult({
      suite: protectionSuite,
      result: result(),
      github,
    });
    await expect(
      buildProtectionResult({
        suite: protectionSuite,
        result: result(),
        github: { ...github, sha: "not-a-sha" },
      }),
    ).rejects.toThrow(CoreValidationError);
    await expect(
      verifyProtectionResult({ ...built, digest: "0".repeat(64) }),
    ).rejects.toThrow(KensaCoreError);
    await expect(
      verifyProtectionResult({ ...built, extra: true }),
    ).rejects.toThrow(CoreValidationError);
  });

  it("syncs suites and protection results idempotently", async () => {
    const protectionSuite = await suite();
    const protectionResult = await buildProtectionResult({
      suite: protectionSuite,
      result: result(),
      github,
    });
    const batch = await buildSyncBatch({
      protection_suites: [protectionSuite],
      protection_results: [protectionResult],
    });

    expect(batch.artifacts.map((artifact) => artifact.kind)).toEqual([
      "protection_result",
      "protection_suite",
    ]);
    expect(await verifySyncBatch(batch)).toEqual(batch);
    const plan = await previewSync(batch, []);
    const receipt = await buildSyncReceipt(plan, plan.upload);
    const repeated = await previewSync(batch, receipt.state);
    expect(repeated.upload).toEqual([]);
    expect(repeated.unchanged).toEqual(receipt.state);
    await expect(
      buildSyncBatch({
        protection_suites: [protectionSuite, protectionSuite],
      }),
    ).rejects.toThrow("duplicate key");
    await expect(
      buildSyncBatch({
        protection_results: [protectionResult, protectionResult],
      }),
    ).rejects.toThrow("duplicate key");
  });
});
