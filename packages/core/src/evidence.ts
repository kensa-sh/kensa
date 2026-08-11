import { z } from "zod";

import { KensaCoreError, parseInput } from "./errors.js";
import {
  canonicalJson,
  digestJson,
  jsonObjectSchema,
  jsonValueSchema,
  type JsonValue,
} from "./json.js";

const traceStatuses = ["ok", "error", "unknown"] as const;
const unixNanoSchema = z
  .union([
    z.string().regex(/^\d+$/),
    z.number().int().nonnegative().refine(Number.isSafeInteger),
  ])
  .nullable()
  .transform((value) => (value === null ? null : BigInt(value).toString()));
const optionalStringSchema = z.string().nullable();
const tokenCountSchema = z.number().int().nonnegative().safe().nullable();

const traceSourceSchema = z.strictObject({
  provider: z.string().min(1),
  import_run_id: z.string().min(1),
  imported_at: z.string().min(1),
});

const usageSchema = z.strictObject({
  model_provider: optionalStringSchema,
  model: optionalStringSchema,
  input_tokens: tokenCountSchema,
  output_tokens: tokenCountSchema,
  total_tokens: tokenCountSchema,
  cache_read_input_tokens: tokenCountSchema,
  cache_creation_input_tokens: tokenCountSchema,
  cost_usd: z.number().finite().nonnegative().nullable(),
});

const spanSchema = z
  .strictObject({
    id: z.string().min(1),
    trace_id: z.string().min(1),
    parent_id: optionalStringSchema,
    name: z.string().min(1),
    kind: z.string().min(1),
    tool_name: optionalStringSchema,
    started_at_unix_nano: unixNanoSchema,
    ended_at_unix_nano: unixNanoSchema,
    duration_ms: z.number().finite().nonnegative(),
    status: z.enum(traceStatuses),
    status_message: optionalStringSchema,
    input: jsonValueSchema,
    output: jsonValueSchema,
    usage: usageSchema,
  })
  .superRefine((span, context) => {
    validateTimeOrder(
      span.started_at_unix_nano,
      span.ended_at_unix_nano,
      context,
    );
  });

const traceSchema = z
  .strictObject({
    schema_version: z.literal("kensa.trace_view.v2"),
    id: z.string().min(1),
    name: optionalStringSchema,
    source: traceSourceSchema,
    started_at_unix_nano: unixNanoSchema,
    ended_at_unix_nano: unixNanoSchema,
    duration_ms: z.number().finite().nonnegative(),
    status: z.enum(traceStatuses),
    input: jsonValueSchema,
    output: jsonValueSchema,
    spans: z.array(spanSchema),
  })
  .superRefine((trace, context) => {
    validateTimeOrder(
      trace.started_at_unix_nano,
      trace.ended_at_unix_nano,
      context,
    );
    const spanIds = new Set<string>();
    for (const [index, span] of trace.spans.entries()) {
      if (spanIds.has(span.id)) {
        addIssue(
          context,
          ["spans", index, "id"],
          "trace contains duplicate span IDs",
        );
      }
      spanIds.add(span.id);
      if (span.trace_id !== trace.id) {
        addIssue(
          context,
          ["spans", index, "trace_id"],
          "span trace ID contradicts its trace",
        );
      }
    }
    for (const [index, span] of trace.spans.entries()) {
      if (span.parent_id === span.id) {
        addIssue(
          context,
          ["spans", index, "parent_id"],
          "span cannot parent itself",
        );
      } else if (span.parent_id !== null && !spanIds.has(span.parent_id)) {
        addIssue(
          context,
          ["spans", index, "parent_id"],
          "span parent is not present in its trace",
        );
      }
    }
    const parents = new Map(
      trace.spans.map((span) => [span.id, span.parent_id] as const),
    );
    for (const [index, span] of trace.spans.entries()) {
      const visited = new Set([span.id]);
      let parentId = span.parent_id;
      while (parentId !== null) {
        if (visited.has(parentId)) {
          addIssue(
            context,
            ["spans", index, "parent_id"],
            "span ancestry contains a cycle",
          );
          break;
        }
        visited.add(parentId);
        parentId = parents.get(parentId) ?? null;
      }
    }
  });

const traceViewsSchema = z.array(traceSchema).superRefine((traces, context) => {
  const ids = new Set<string>();
  for (const [index, trace] of traces.entries()) {
    if (ids.has(trace.id)) {
      addIssue(context, [index, "id"], "trace views contain duplicate IDs");
    }
    ids.add(trace.id);
  }
});
const sourceIdentitySchema = z.strictObject({
  schema_version: z.literal("kensa.source_identity.v1"),
  kind: z.literal("trace"),
  provider: z.string().min(1),
  source_id: z.string().regex(/^trace_[0-9a-f]{24}$/),
  digest: z.string().regex(/^[0-9a-f]{64}$/),
});
const evidenceRecordSchema = z.strictObject({
  schema_version: z.literal("kensa.evidence.v1"),
  identity: sourceIdentitySchema,
  trace: traceSchema,
});
export const REDACTION_RULESET_HASH =
  "96332f6e9bdb07b0d837e733c393f3e4dd2ecd0b8e910fff06ba6266114ae422";
const redactionProofSchema = z.strictObject({
  version: z.literal("kensa.redactor.v2"),
  mandatory: z.literal(true),
  language: z.literal("en"),
  value_redaction_applied: z.literal(true),
  redaction_available: z.literal(true),
  redacted_span_count: z.number().int().nonnegative(),
  changed_value_count: z.number().int().nonnegative(),
  secret_keys_redacted: z.boolean(),
  trace_count: z.number().int().nonnegative(),
  ruleset_hash: z.literal(REDACTION_RULESET_HASH),
  pseudonymization: z.literal("instance-counter"),
  entity_instance_counts: z.record(z.string(), z.number().int().nonnegative()),
  detectors: jsonObjectSchema,
  model: z.strictObject({
    name: z.enum(["en_core_web_sm", "en_core_web_lg"]),
    version: z.literal("3.8.0"),
    checksum_verified: z.literal(true),
  }),
});
const redactedEvidenceRecordSchema = z.strictObject({
  schema_version: z.literal("kensa.redacted_evidence.v1"),
  identity: sourceIdentitySchema,
  redaction: redactionProofSchema,
  trace: traceSchema,
  digest: z.string().regex(/^[0-9a-f]{64}$/),
});

export type TraceStatus = (typeof traceStatuses)[number];
export type TraceSource = z.infer<typeof traceSourceSchema>;
export type TraceUsage = z.infer<typeof usageSchema>;
export type TraceSpan = z.infer<typeof spanSchema>;
export type TraceView = z.infer<typeof traceSchema>;

export interface SourceIdentity {
  schema_version: "kensa.source_identity.v1";
  kind: "trace";
  provider: string;
  source_id: string;
  digest: string;
}

export interface EvidenceRecord {
  schema_version: "kensa.evidence.v1";
  identity: SourceIdentity;
  trace: TraceView;
}

export type RedactionProof = z.infer<typeof redactionProofSchema>;

export interface RedactedEvidenceRecord {
  schema_version: "kensa.redacted_evidence.v1";
  identity: SourceIdentity;
  redaction: RedactionProof;
  trace: TraceView;
  digest: string;
}

export function parseTraceView(input: unknown): TraceView {
  return parseInput(
    traceSchema,
    input,
    "trace view violates the core contract",
  );
}

export function parseTraceViews(input: unknown): TraceView[] {
  return parseInput(
    traceViewsSchema,
    input,
    "trace views violate the core contract",
  );
}

export function parseEvidenceRecord(input: unknown): EvidenceRecord {
  return parseInput(
    evidenceRecordSchema,
    input,
    "evidence record violates the core contract",
  );
}

export function parseRedactionProof(input: unknown): RedactionProof {
  return parseInput(
    redactionProofSchema,
    input,
    "redaction proof violates the core contract",
  );
}

export function parseRedactedEvidenceRecord(
  input: unknown,
): RedactedEvidenceRecord {
  return parseInput(
    redactedEvidenceRecordSchema,
    input,
    "redacted evidence record violates the core contract",
  );
}

export function normalizeTraceView(input: unknown): TraceView {
  const trace = parseTraceView(input);
  const spans = trace.spans.map(normalizeSpan).sort(compareSpans);
  const startedAt = trace.started_at_unix_nano ?? minimumTimestamp(spans);
  const endedAt = trace.ended_at_unix_nano ?? maximumTimestamp(spans);
  return {
    ...trace,
    started_at_unix_nano: startedAt,
    ended_at_unix_nano: endedAt,
    duration_ms: durationMs(startedAt, endedAt),
    status: aggregateStatus(trace.status, spans),
    spans,
  };
}

export function normalizeTraceViews(input: unknown): TraceView[] {
  return parseTraceViews(input)
    .map(normalizeTraceView)
    .sort((left, right) => compareText(left.id, right.id));
}

export async function sourceIdentity(input: unknown): Promise<SourceIdentity> {
  const trace = normalizeTraceView(input);
  const digest = await digestJson({
    ...trace,
    source: { provider: trace.source.provider },
  });
  return {
    schema_version: "kensa.source_identity.v1",
    kind: "trace",
    provider: trace.source.provider,
    source_id: `trace_${digest.slice(0, 24)}`,
    digest,
  };
}

export async function buildEvidenceRecord(
  input: unknown,
): Promise<EvidenceRecord> {
  const trace = normalizeTraceView(input);
  return {
    schema_version: "kensa.evidence.v1",
    identity: await sourceIdentity(trace),
    trace,
  };
}

export async function verifyEvidenceRecord(
  input: unknown,
): Promise<EvidenceRecord> {
  const record = parseEvidenceRecord(input);
  const trace = normalizeTraceView(record.trace);
  const expected = await sourceIdentity(trace);
  if (canonicalJson(record.identity) !== canonicalJson(expected)) {
    throw new KensaCoreError(
      "invalid_input",
      "evidence identity does not match its trace",
    );
  }
  return { ...record, trace };
}

export async function buildRedactedEvidenceRecord(
  traceInput: unknown,
  proofInput: unknown,
): Promise<RedactedEvidenceRecord> {
  const trace = normalizeTraceView(traceInput);
  const identity = await sourceIdentity(trace);
  const redaction = parseRedactionProof(proofInput);
  return {
    schema_version: "kensa.redacted_evidence.v1",
    identity,
    redaction,
    trace,
    digest: await digestJson({ identity, redaction, trace }),
  };
}

export async function verifyRedactedEvidenceRecord(
  input: unknown,
): Promise<RedactedEvidenceRecord> {
  const record = parseRedactedEvidenceRecord(input);
  const expected = await buildRedactedEvidenceRecord(
    record.trace,
    record.redaction,
  );
  if (canonicalJson(record) !== canonicalJson(expected)) {
    throw new KensaCoreError(
      "invalid_input",
      "redacted evidence proof does not match its trace",
    );
  }
  return expected;
}

function normalizeSpan(span: TraceSpan): TraceSpan {
  return {
    ...span,
    duration_ms: durationMs(span.started_at_unix_nano, span.ended_at_unix_nano),
  };
}

function validateTimeOrder(
  startedAt: string | null,
  endedAt: string | null,
  context: z.RefinementCtx,
): void {
  if (
    startedAt !== null &&
    endedAt !== null &&
    BigInt(endedAt) < BigInt(startedAt)
  ) {
    addIssue(context, ["ended_at_unix_nano"], "end time precedes start time");
  }
}

function durationMs(startedAt: string | null, endedAt: string | null): number {
  if (startedAt === null || endedAt === null) return 0;
  const duration = Number(BigInt(endedAt) - BigInt(startedAt)) / 1_000_000;
  if (!Number.isFinite(duration)) {
    throw new KensaCoreError(
      "invalid_input",
      "trace duration exceeds the interoperable number range",
    );
  }
  return duration;
}

function minimumTimestamp(spans: TraceSpan[]): string | null {
  const values = spans
    .map((span) => span.started_at_unix_nano)
    .filter((value): value is string => value !== null);
  return values[0] ?? null;
}

function maximumTimestamp(spans: TraceSpan[]): string | null {
  const values = spans
    .map((span) => span.ended_at_unix_nano)
    .filter((value): value is string => value !== null);
  if (values.length === 0) return null;
  return values.reduce((selected, value) => {
    return BigInt(value) > BigInt(selected) ? value : selected;
  });
}

function aggregateStatus(status: TraceStatus, spans: TraceSpan[]): TraceStatus {
  if (status !== "unknown") return status;
  if (spans.some((span) => span.status === "error")) return "error";
  if (spans.length > 0 && spans.every((span) => span.status === "ok"))
    return "ok";
  return "unknown";
}

function compareSpans(left: TraceSpan, right: TraceSpan): number {
  return compareNullableTimestamps(
    left.started_at_unix_nano,
    right.started_at_unix_nano,
  );
}

function compareNullableTimestamps(
  left: string | null,
  right: string | null,
): number {
  if (left === right) return 0;
  if (left === null) return 1;
  if (right === null) return -1;
  return BigInt(left) < BigInt(right) ? -1 : 1;
}

function compareText(left: string, right: string): number {
  return left < right ? -1 : 1;
}

function addIssue(
  context: z.RefinementCtx,
  path: Array<string | number>,
  message: string,
): void {
  context.addIssue({ code: "custom", path, message });
}
