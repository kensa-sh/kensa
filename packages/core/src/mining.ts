import { z } from "zod";

import { KensaCoreError, parseInput } from "./errors.js";
import {
  normalizeTraceViews,
  verifyRedactedEvidenceRecord,
  type RedactedEvidenceRecord,
  type SourceIdentity,
  type TraceStatus,
  type TraceView,
} from "./evidence.js";
import { canonicalJson, digestJson } from "./json.js";

const inspectStatuses = [
  "pending",
  "approved",
  "rejected",
  "generated",
] as const;
const expectedBehaviors = ["pass", "fail"] as const;
const trimmedString = z.string().trim().min(1);
const inspectIdeaSchema = z.strictObject({
  id: z
    .string()
    .trim()
    .regex(/^[a-z0-9][a-z0-9-]{0,63}$/),
  trace_ids: z.array(trimmedString).min(1),
  source: trimmedString,
  status: z.enum(inspectStatuses).default("pending"),
  failure_pattern: trimmedString,
  expected_outcome: trimmedString,
  expected_current_behavior: z.enum(expectedBehaviors),
  proposed_checks: z.array(trimmedString).default([]),
  case_shape: trimmedString.nullable().default(null),
  risks: trimmedString.nullable().default(null),
});
const inspectQueueSchema = z
  .strictObject({
    schema_version: z.literal("kensa.inspect.v1").default("kensa.inspect.v1"),
    items: z.array(inspectIdeaSchema).default([]),
  })
  .superRefine((queue, context) => {
    const ids = new Set<string>();
    for (const [index, item] of queue.items.entries()) {
      if (ids.has(item.id)) {
        addIssue(
          context,
          ["items", index, "id"],
          "inspect queue contains duplicate item IDs",
        );
      }
      ids.add(item.id);
      if (new Set(item.trace_ids).size !== item.trace_ids.length) {
        addIssue(
          context,
          ["items", index, "trace_ids"],
          "inspect item contains duplicate trace IDs",
        );
      }
    }
  });

export type InspectStatus = (typeof inspectStatuses)[number];
export type ExpectedCurrentBehavior = (typeof expectedBehaviors)[number];
export type InspectIdea = z.infer<typeof inspectIdeaSchema>;
export type InspectQueue = z.infer<typeof inspectQueueSchema>;

export interface TraceSummary {
  id: string;
  name: string | null;
  status: TraceStatus;
  started_at_unix_nano: string | null;
  duration_ms: number;
  span_count: number;
  source: { provider: string };
}

export interface BehaviorCluster {
  schema_version: "kensa.behavior.v1";
  id: string;
  digest: string;
  name: string | null;
  status: TraceStatus;
  tool_sequence: string[];
  error_messages: string[];
  trace_ids: string[];
  evidence_digest: string;
  occurrences: number;
}

export interface CandidateEvidence {
  identity: SourceIdentity;
  record_digest: string;
}

export interface BoundInspectCandidate {
  item: InspectIdea;
  evidence: CandidateEvidence[];
  digest: string;
}

export interface CandidateSet {
  schema_version: "kensa.candidate_set.v1";
  candidates: BoundInspectCandidate[];
  digest: string;
}

export function parseInspectQueue(input: unknown): InspectQueue {
  return parseInput(
    inspectQueueSchema,
    input,
    "inspect queue violates the core contract",
  );
}

export function traceSummary(input: unknown): TraceSummary {
  const trace = normalizeTraceViews([input])[0]!;
  return {
    id: trace.id,
    name: trace.name,
    status: trace.status,
    started_at_unix_nano: trace.started_at_unix_nano,
    duration_ms: trace.duration_ms,
    span_count: trace.spans.length,
    source: { provider: trace.source.provider },
  };
}

export async function mineTraceBehaviors(
  input: unknown,
): Promise<BehaviorCluster[]> {
  const records = await verifiedEvidence(input);
  const groups = new Map<
    string,
    { signature: BehaviorSignature; records: RedactedEvidenceRecord[] }
  >();
  for (const record of records) {
    const trace = record.trace;
    const signature = behaviorSignature(trace);
    const key = canonicalJson(signature);
    const group = groups.get(key) ?? { signature, records: [] };
    group.records.push(record);
    groups.set(key, group);
  }
  const clusters = await Promise.all(
    [...groups.values()].map(async ({ signature, records: grouped }) => {
      const digest = await digestJson(signature);
      return {
        schema_version: "kensa.behavior.v1" as const,
        id: `behavior_${digest.slice(0, 24)}`,
        digest,
        ...signature,
        trace_ids: grouped.map((record) => record.trace.id).sort(compareText),
        evidence_digest: await digestJson(
          grouped.map((record) => record.digest).sort(compareText),
        ),
        occurrences: grouped.length,
      };
    }),
  );
  return clusters.sort((left, right) => compareText(left.id, right.id));
}

export async function bindInspectCandidates(
  queueInput: unknown,
  evidenceInput: unknown,
): Promise<CandidateSet> {
  const queue = parseInspectQueue(queueInput);
  const records = await verifiedEvidence(evidenceInput);
  const byId = new Map(
    records.map((record) => [record.trace.id, record] as const),
  );
  const candidates = await Promise.all(
    queue.items.map(async (item) => {
      const evidence = item.trace_ids.map((traceId) => {
        const record = byId.get(traceId);
        if (record === undefined) {
          throw new KensaCoreError(
            "invalid_input",
            `inspect item ${item.id} references unknown trace ${traceId}`,
          );
        }
        return {
          identity: record.identity,
          record_digest: record.digest,
        };
      });
      evidence.sort((left, right) =>
        compareText(left.identity.source_id, right.identity.source_id),
      );
      return {
        item,
        evidence,
        digest: await digestJson({ item, evidence }),
      };
    }),
  );
  candidates.sort((left, right) => compareText(left.item.id, right.item.id));
  return {
    schema_version: "kensa.candidate_set.v1",
    candidates,
    digest: await digestJson(candidates),
  };
}

async function verifiedEvidence(
  input: unknown,
): Promise<RedactedEvidenceRecord[]> {
  const records = parseInput(
    z.array(z.unknown()),
    input,
    "redacted evidence records must be an array",
  );
  const verified = await Promise.all(records.map(verifyRedactedEvidenceRecord));
  const traceIds = new Set<string>();
  for (const record of verified) {
    if (traceIds.has(record.trace.id)) {
      throw new KensaCoreError(
        "invalid_input",
        `redacted evidence contains duplicate trace ${record.trace.id}`,
      );
    }
    traceIds.add(record.trace.id);
  }
  return verified.sort((left, right) =>
    compareText(left.identity.source_id, right.identity.source_id),
  );
}

interface BehaviorSignature {
  name: string | null;
  status: TraceStatus;
  tool_sequence: string[];
  error_messages: string[];
}

function behaviorSignature(trace: TraceView): BehaviorSignature {
  return {
    name: trace.name,
    status: trace.status,
    tool_sequence: trace.spans.flatMap((span) =>
      span.tool_name === null ? [] : [span.tool_name],
    ),
    error_messages: trace.spans.flatMap((span) =>
      span.status === "error" && span.status_message !== null
        ? [span.status_message]
        : [],
    ),
  };
}

function compareText(left: string, right: string): number {
  return Number(left > right) - Number(left < right);
}

function addIssue(
  context: z.RefinementCtx,
  path: Array<string | number>,
  message: string,
): void {
  context.addIssue({ code: "custom", path, message });
}
