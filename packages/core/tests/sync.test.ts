import { describe, expect, it } from "vitest";

import {
  buildRedactedEvidenceRecord,
  buildRunResult,
  buildSyncBatch,
  buildSyncReceipt,
  CoreValidationError,
  KensaCoreError,
  previewSync,
  verifySyncBatch,
  verifySyncPlan,
  verifySyncReceipt,
} from "../src/index.js";

const redactionProof = {
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

function trace(id: string): Record<string, unknown> {
  return {
    schema_version: "kensa.trace_view.v2",
    id,
    name: null,
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

function result(runId: string) {
  return buildRunResult({
    run_id: runId,
    complete: true,
    interruption: null,
    trials: [],
  });
}

async function evidence(id: string) {
  return buildRedactedEvidenceRecord(trace(id), redactionProof);
}

async function batch() {
  return buildSyncBatch({
    evidence: [await evidence("trace-b"), await evidence("trace-a")],
    results: [result("run-b"), result("run-a")],
  });
}

describe("portable sync contracts", () => {
  it("builds and verifies deterministic evidence and result batches", async () => {
    const first = await batch();
    const second = await buildSyncBatch({
      evidence: [await evidence("trace-a"), await evidence("trace-b")],
      results: [result("run-a"), result("run-b")],
    });

    expect(first).toEqual(second);
    expect(first.schema_version).toBe("kensa.sync_batch.v1");
    expect(first.artifacts.map((artifact) => artifact.key)).toEqual(
      [...first.artifacts.map((artifact) => artifact.key)].sort(),
    );
    expect(first.digest).toMatch(/^[0-9a-f]{64}$/);
    expect(await verifySyncBatch(first)).toEqual(first);
    expect(await buildSyncBatch({})).toMatchObject({ artifacts: [] });
  });

  it("rejects malformed, tampered, and duplicate batch artifacts", async () => {
    const built = await batch();
    await expect(
      verifySyncBatch({ ...built, digest: "0".repeat(64) }),
    ).rejects.toThrow(KensaCoreError);
    await expect(verifySyncBatch({ ...built, extra: true })).rejects.toThrow(
      CoreValidationError,
    );
    await expect(buildSyncBatch({ evidence: [trace("raw")] })).rejects.toThrow(
      CoreValidationError,
    );
    const duplicate = await evidence("same");
    await expect(
      buildSyncBatch({ evidence: [duplicate, duplicate] }),
    ).rejects.toThrow("duplicate key");
    await expect(
      buildSyncBatch({ results: [result("same"), result("same")] }),
    ).rejects.toThrow("duplicate key");
  });

  it("previews uploads, unchanged artifacts, and immutable-key conflicts", async () => {
    const built = await batch();
    const [first, second, third] = built.artifacts;
    const retained = { key: "result:remote-only", digest: "d".repeat(64) };
    const plan = await previewSync(built, [
      retained,
      { key: third!.key, digest: "e".repeat(64) },
      { key: second!.key, digest: "f".repeat(64) },
      { key: first!.key, digest: first!.digest },
    ]);

    expect(plan.upload).toHaveLength(1);
    expect(plan.unchanged).toEqual([
      { key: first!.key, digest: first!.digest },
    ]);
    expect(plan.conflicts).toEqual([
      {
        key: second!.key,
        local_digest: second!.digest,
        remote_digest: "f".repeat(64),
      },
      {
        key: third!.key,
        local_digest: third!.digest,
        remote_digest: "e".repeat(64),
      },
    ]);
    expect(plan.retained).toEqual([retained]);
    expect(await verifySyncPlan(plan)).toEqual(plan);
    await expect(buildSyncReceipt(plan, plan.upload)).rejects.toThrow(
      "conflicts",
    );
  });

  it("creates a self-verifying receipt and makes repeated sync a no-op", async () => {
    const built = await batch();
    const retained = { key: "result:remote-only", digest: "d".repeat(64) };
    const plan = await previewSync(built, [retained]);
    const receipt = await buildSyncReceipt(plan, [...plan.upload].reverse());

    expect(receipt.applied).toEqual(plan.upload);
    expect(receipt.plan_digest).toBe(plan.digest);
    expect(receipt.unchanged).toEqual([]);
    expect(receipt.retained).toEqual([retained]);
    expect(receipt.state).toHaveLength(plan.upload.length + 1);
    expect(receipt.state).toContainEqual(retained);
    expect(await verifySyncReceipt(receipt)).toEqual(receipt);
    const repeated = await previewSync(built, receipt.state);
    expect(repeated.upload).toEqual([]);
    expect(repeated.conflicts).toEqual([]);
    expect(repeated.unchanged).toEqual(plan.upload);
    expect(repeated.retained).toEqual([retained]);
    const repeatedReceipt = await buildSyncReceipt(repeated, []);
    expect(repeatedReceipt.applied).toEqual([]);
    expect(repeatedReceipt.state).toEqual(receipt.state);
  });

  it("rejects noncanonical plans, receipts, states, and applied sets", async () => {
    const built = await batch();
    const plan = await previewSync(built, []);
    await expect(
      verifySyncPlan({ ...plan, upload: [...plan.upload].reverse() }),
    ).rejects.toThrow("not canonical");
    await expect(
      verifySyncPlan({
        ...plan,
        unchanged: [plan.upload[0]],
        retained: plan.retained,
        digest: "0".repeat(64),
      }),
    ).rejects.toThrow("duplicate key");
    await expect(buildSyncReceipt(plan, plan.upload.slice(1))).rejects.toThrow(
      "do not match",
    );
    await expect(
      previewSync(built, [
        { key: "duplicate", digest: "0".repeat(64) },
        { key: "duplicate", digest: "1".repeat(64) },
      ]),
    ).rejects.toThrow("duplicate key");
    const receipt = await buildSyncReceipt(plan, plan.upload);
    await expect(verifySyncReceipt({ ...receipt, state: [] })).rejects.toThrow(
      "not canonical",
    );
    await expect(
      verifySyncReceipt({
        ...receipt,
        unchanged: [receipt.applied[0]],
        retained: receipt.retained,
        digest: "0".repeat(64),
      }),
    ).rejects.toThrow("duplicate key");
  });
});
