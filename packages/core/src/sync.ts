import { z } from "zod";

import { verifyRunResult, type RunResult } from "./aggregation.js";
import {
  verifyRedactedEvidenceRecord,
  type RedactedEvidenceRecord,
} from "./evidence.js";
import { KensaCoreError, parseInput } from "./errors.js";
import { canonicalJson, digestJson } from "./json.js";

const digestSchema = z.string().regex(/^[0-9a-f]{64}$/);
const syncPointerSchema = z.strictObject({
  key: z.string().min(1),
  digest: digestSchema,
});
const evidenceArtifactSchema = z.strictObject({
  schema_version: z.literal("kensa.sync_artifact.v1"),
  kind: z.literal("evidence"),
  key: z.string().min(1),
  digest: digestSchema,
  payload: z.unknown(),
});
const resultArtifactSchema = z.strictObject({
  schema_version: z.literal("kensa.sync_artifact.v1"),
  kind: z.literal("result"),
  key: z.string().min(1),
  digest: digestSchema,
  payload: z.unknown(),
});
const syncArtifactSchema = z.discriminatedUnion("kind", [
  evidenceArtifactSchema,
  resultArtifactSchema,
]);
const syncBatchSchema = z.strictObject({
  schema_version: z.literal("kensa.sync_batch.v1"),
  artifacts: z.array(syncArtifactSchema),
  digest: digestSchema,
});
const buildSyncBatchInputSchema = z.strictObject({
  evidence: z.array(z.unknown()).default([]),
  results: z.array(z.unknown()).default([]),
});
const syncConflictSchema = z.strictObject({
  key: z.string().min(1),
  local_digest: digestSchema,
  remote_digest: digestSchema,
});
const syncPlanSchema = z.strictObject({
  schema_version: z.literal("kensa.sync_plan.v1"),
  batch_digest: digestSchema,
  upload: z.array(syncPointerSchema),
  unchanged: z.array(syncPointerSchema),
  retained: z.array(syncPointerSchema),
  conflicts: z.array(syncConflictSchema),
  digest: digestSchema,
});
const syncReceiptSchema = z.strictObject({
  schema_version: z.literal("kensa.sync_receipt.v1"),
  batch_digest: digestSchema,
  plan_digest: digestSchema,
  applied: z.array(syncPointerSchema),
  unchanged: z.array(syncPointerSchema),
  retained: z.array(syncPointerSchema),
  state: z.array(syncPointerSchema),
  digest: digestSchema,
});

export interface SyncPointer {
  key: string;
  digest: string;
}

export interface EvidenceSyncArtifact {
  schema_version: "kensa.sync_artifact.v1";
  kind: "evidence";
  key: string;
  digest: string;
  payload: RedactedEvidenceRecord;
}

export interface ResultSyncArtifact {
  schema_version: "kensa.sync_artifact.v1";
  kind: "result";
  key: string;
  digest: string;
  payload: RunResult;
}

export type SyncArtifact = EvidenceSyncArtifact | ResultSyncArtifact;

export interface SyncBatch {
  schema_version: "kensa.sync_batch.v1";
  artifacts: SyncArtifact[];
  digest: string;
}

export interface SyncConflict {
  key: string;
  local_digest: string;
  remote_digest: string;
}

export interface SyncPlan {
  schema_version: "kensa.sync_plan.v1";
  batch_digest: string;
  upload: SyncPointer[];
  unchanged: SyncPointer[];
  retained: SyncPointer[];
  conflicts: SyncConflict[];
  digest: string;
}

export interface SyncReceipt {
  schema_version: "kensa.sync_receipt.v1";
  batch_digest: string;
  plan_digest: string;
  applied: SyncPointer[];
  unchanged: SyncPointer[];
  retained: SyncPointer[];
  state: SyncPointer[];
  digest: string;
}

export async function buildSyncBatch(input: unknown): Promise<SyncBatch> {
  const parsed = parseInput(
    buildSyncBatchInputSchema,
    input,
    "sync batch input violates the core contract",
  );
  const evidence = await Promise.all(
    parsed.evidence.map(verifyRedactedEvidenceRecord),
  );
  const results = parsed.results.map(verifyRunResult);
  const artifacts = await Promise.all<SyncArtifact>([
    ...evidence.map(buildEvidenceArtifact),
    ...results.map(buildResultArtifact),
  ]);
  artifacts.sort(compareArtifacts);
  rejectDuplicateKeys(artifacts);
  return withDigest({ schema_version: "kensa.sync_batch.v1", artifacts });
}

export async function verifySyncBatch(input: unknown): Promise<SyncBatch> {
  const batch = parseInput(
    syncBatchSchema,
    input,
    "sync batch violates the core contract",
  );
  const expected = await buildSyncBatch({
    evidence: batch.artifacts
      .filter((artifact) => artifact.kind === "evidence")
      .map((artifact) => artifact.payload),
    results: batch.artifacts
      .filter((artifact) => artifact.kind === "result")
      .map((artifact) => artifact.payload),
  });
  requireCanonical(input, expected, "sync batch does not match its artifacts");
  return expected;
}

export async function previewSync(
  batchInput: unknown,
  remoteStateInput: unknown,
): Promise<SyncPlan> {
  const batch = await verifySyncBatch(batchInput);
  const remote = normalizePointers(remoteStateInput, "remote sync state");
  const byKey = new Map(remote.map((pointer) => [pointer.key, pointer]));
  const upload: SyncPointer[] = [];
  const unchanged: SyncPointer[] = [];
  const conflicts: SyncConflict[] = [];
  const batchKeys = new Set(batch.artifacts.map((artifact) => artifact.key));
  for (const artifact of batch.artifacts) {
    const pointer = artifactPointer(artifact);
    const existing = byKey.get(artifact.key);
    if (existing === undefined) {
      upload.push(pointer);
    } else if (existing.digest === artifact.digest) {
      unchanged.push(pointer);
    } else {
      conflicts.push({
        key: artifact.key,
        local_digest: artifact.digest,
        remote_digest: existing.digest,
      });
    }
  }
  const retained = remote.filter((pointer) => !batchKeys.has(pointer.key));
  return withDigest({
    schema_version: "kensa.sync_plan.v1",
    batch_digest: batch.digest,
    upload,
    unchanged,
    retained,
    conflicts,
  });
}

export async function verifySyncPlan(input: unknown): Promise<SyncPlan> {
  const plan = parseInput(
    syncPlanSchema,
    input,
    "sync plan violates the core contract",
  );
  const expected = await withDigest({
    schema_version: "kensa.sync_plan.v1" as const,
    batch_digest: plan.batch_digest,
    upload: normalizePointers(plan.upload, "sync plan uploads"),
    unchanged: normalizePointers(
      plan.unchanged,
      "sync plan unchanged artifacts",
    ),
    retained: normalizePointers(plan.retained, "sync plan retained artifacts"),
    conflicts: normalizeConflicts(plan.conflicts),
  });
  rejectOverlappingKeys(
    expected.upload,
    expected.unchanged,
    expected.retained,
    expected.conflicts,
  );
  requireCanonical(input, expected, "sync plan is not canonical");
  return expected;
}

export async function buildSyncReceipt(
  planInput: unknown,
  appliedInput: unknown,
): Promise<SyncReceipt> {
  const plan = await verifySyncPlan(planInput);
  if (plan.conflicts.length > 0) {
    throw new KensaCoreError(
      "invalid_input",
      "sync plan conflicts must be resolved before receipt creation",
    );
  }
  const applied = normalizePointers(appliedInput, "applied sync artifacts");
  if (canonicalJson(applied) !== canonicalJson(plan.upload)) {
    throw new KensaCoreError(
      "invalid_input",
      "applied sync artifacts do not match the upload plan",
    );
  }
  const unchanged = [...plan.unchanged];
  const retained = [...plan.retained];
  const state = normalizePointers(
    [...applied, ...unchanged, ...retained],
    "sync receipt state",
  );
  return withDigest({
    schema_version: "kensa.sync_receipt.v1",
    batch_digest: plan.batch_digest,
    plan_digest: plan.digest,
    applied,
    unchanged,
    retained,
    state,
  });
}

export async function verifySyncReceipt(input: unknown): Promise<SyncReceipt> {
  const receipt = parseInput(
    syncReceiptSchema,
    input,
    "sync receipt violates the core contract",
  );
  const applied = normalizePointers(
    receipt.applied,
    "sync receipt applied artifacts",
  );
  const unchanged = normalizePointers(
    receipt.unchanged,
    "sync receipt unchanged artifacts",
  );
  const retained = normalizePointers(
    receipt.retained,
    "sync receipt retained artifacts",
  );
  rejectOverlappingKeys(applied, unchanged, retained, []);
  const expected = await withDigest({
    schema_version: "kensa.sync_receipt.v1" as const,
    batch_digest: receipt.batch_digest,
    plan_digest: receipt.plan_digest,
    applied,
    unchanged,
    retained,
    state: normalizePointers(
      [...applied, ...unchanged, ...retained],
      "sync receipt state",
    ),
  });
  requireCanonical(input, expected, "sync receipt is not canonical");
  return expected;
}

async function buildEvidenceArtifact(
  payload: RedactedEvidenceRecord,
): Promise<EvidenceSyncArtifact> {
  return {
    schema_version: "kensa.sync_artifact.v1",
    kind: "evidence",
    key: `evidence:${payload.identity.source_id}`,
    digest: payload.digest,
    payload,
  };
}

async function buildResultArtifact(
  payload: RunResult,
): Promise<ResultSyncArtifact> {
  return {
    schema_version: "kensa.sync_artifact.v1",
    kind: "result",
    key: `result:${payload.run_id}`,
    digest: await digestJson(payload),
    payload,
  };
}

function normalizePointers(input: unknown, label: string): SyncPointer[] {
  const pointers = parseInput(
    z.array(syncPointerSchema),
    input,
    `${label} violates the core contract`,
  ).sort(comparePointers);
  rejectDuplicateKeys(pointers);
  return pointers;
}

function normalizeConflicts(input: unknown): SyncConflict[] {
  const conflicts = parseInput(
    z.array(syncConflictSchema),
    input,
    "sync conflicts violate the core contract",
  ).sort((left, right) => compareText(left.key, right.key));
  rejectDuplicateKeys(conflicts);
  return conflicts;
}

function rejectDuplicateKeys(items: Array<{ key: string }>): void {
  const keys = new Set<string>();
  for (const item of items) {
    if (keys.has(item.key)) {
      throw new KensaCoreError(
        "invalid_input",
        `sync data contains duplicate key ${item.key}`,
      );
    }
    keys.add(item.key);
  }
}

function rejectOverlappingKeys(
  first: SyncPointer[],
  second: SyncPointer[],
  third: SyncPointer[],
  conflicts: SyncConflict[],
): void {
  rejectDuplicateKeys([...first, ...second, ...third, ...conflicts]);
}

function artifactPointer(artifact: SyncArtifact): SyncPointer {
  return { key: artifact.key, digest: artifact.digest };
}

function compareArtifacts(left: SyncArtifact, right: SyncArtifact): number {
  return compareText(left.key, right.key);
}

function comparePointers(left: SyncPointer, right: SyncPointer): number {
  return compareText(left.key, right.key);
}

function compareText(left: string, right: string): number {
  return Number(left > right) - Number(left < right);
}

async function withDigest<T extends { schema_version: string }>(
  value: T,
): Promise<T & { digest: string }> {
  return { ...value, digest: await digestJson(value) };
}

function requireCanonical(
  input: unknown,
  expected: unknown,
  message: string,
): void {
  if (canonicalJson(input) !== canonicalJson(expected)) {
    throw new KensaCoreError("invalid_input", message);
  }
}
