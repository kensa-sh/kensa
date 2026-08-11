import { z } from "zod";
import { ProtocolError, type ProtocolErrorCode } from "./errors.js";
import {
  caseIdSchema,
  checkResultIdSchema,
  evalRunIdSchema,
  invocationIdSchema,
  nonBlankStringSchema,
  safeIntegerSchema,
  spanIdSchema,
  traceIdSchema,
  u32Schema,
} from "./identifiers.js";
import {
  invalidJsonPath,
  isJsonObject,
  isJsonValue,
  type JsonObject,
  type JsonValue,
} from "./json.js";
import { timestampSchema } from "./timestamp.js";

export const SCHEMA_VERSION = "kensa.protocol.v1" as const;
export const DOCUMENT_KINDS = [
  "eval_run",
  "invocation",
  "span",
  "check_result",
] as const;
const jsonValueSchema = z.json().refine(isJsonValue, {
  message: "Value is not JSON-compatible",
  params: { kensaCode: "invalid_json_value" },
}) as z.ZodType<JsonValue>;
const jsonObjectSchema = z.record(z.string(), z.json()).refine(isJsonObject, {
  message: "Value must be a JSON object",
  params: { kensaCode: "invalid_json_value" },
}) as z.ZodType<JsonObject>;
const failureCategorySchema = z.enum([
  "agent",
  "simulator",
  "judge",
  "configuration",
  "infrastructure",
  "harness",
  "unknown",
]);
const effectPolicySchema = z.enum(["none", "captured", "sandboxed", "live"]);
const evidenceStatusSchema = z.enum([
  "complete",
  "pending",
  "partial",
  "unavailable",
]);
const failureSchema = z.strictObject({
  category: failureCategorySchema,
  kind: nonBlankStringSchema,
  message: nonBlankStringSchema,
  evidence: jsonObjectSchema,
});
const provenanceSchema = z.strictObject({
  producer: nonBlankStringSchema,
  producer_version: nonBlankStringSchema,
  adapter: nonBlankStringSchema.nullable(),
  adapter_version: nonBlankStringSchema.nullable(),
  runtime: nonBlankStringSchema,
  runtime_version: nonBlankStringSchema,
  revision: nonBlankStringSchema.nullable(),
  environment: nonBlankStringSchema.nullable(),
  effects: effectPolicySchema,
});
const completenessSchema = z.strictObject({
  status: evidenceStatusSchema,
  reason: nonBlankStringSchema.nullable(),
});
const caseSnapshotSchema = z.strictObject({
  id: caseIdSchema,
  input: jsonValueSchema,
  metadata: jsonObjectSchema,
});
const common = { schema_version: z.literal(SCHEMA_VERSION) };

const evalRunBase = z.strictObject({
  ...common,
  document_kind: z.literal("eval_run"),
  id: evalRunIdSchema,
  status: z.enum([
    "pending",
    "running",
    "pass",
    "fail",
    "error",
    "cancelled",
    "interrupted",
  ]),
  created_at: timestampSchema,
  started_at: timestampSchema.nullable(),
  ended_at: timestampSchema.nullable(),
  duration_ms: safeIntegerSchema.nullable(),
  attributes: jsonObjectSchema,
  failure: failureSchema.nullable(),
});
const invocationBase = z.strictObject({
  ...common,
  document_kind: z.literal("invocation"),
  id: invocationIdSchema,
  run_id: evalRunIdSchema,
  case: caseSnapshotSchema,
  attempt: u32Schema.min(1),
  status: z.enum([
    "pending",
    "running",
    "pass",
    "fail",
    "error",
    "cancelled",
    "skipped",
    "interrupted",
  ]),
  started_at: timestampSchema.nullable(),
  ended_at: timestampSchema.nullable(),
  duration_ms: safeIntegerSchema.nullable(),
  output_recorded: z.boolean(),
  output: jsonValueSchema.nullable(),
  provenance: provenanceSchema,
  evidence_completeness: completenessSchema,
  attributes: jsonObjectSchema,
  failure: failureSchema.nullable(),
});
const spanBase = z.strictObject({
  ...common,
  document_kind: z.literal("span"),
  invocation_id: invocationIdSchema,
  trace_id: traceIdSchema,
  span_id: spanIdSchema,
  parent_span_id: spanIdSchema.nullable(),
  name: nonBlankStringSchema,
  span_kind: nonBlankStringSchema,
  status: z.enum(["unset", "ok", "error"]),
  status_message: nonBlankStringSchema.nullable(),
  started_at: timestampSchema.nullable(),
  ended_at: timestampSchema.nullable(),
  duration_ms: safeIntegerSchema.nullable(),
  input_recorded: z.boolean(),
  input: jsonValueSchema.nullable(),
  output_recorded: z.boolean(),
  output: jsonValueSchema.nullable(),
  attributes: jsonObjectSchema,
});
const checkResultBase = z.strictObject({
  ...common,
  document_kind: z.literal("check_result"),
  id: checkResultIdSchema,
  invocation_id: invocationIdSchema,
  name: nonBlankStringSchema,
  status: z.enum(["pass", "fail", "error", "skipped"]),
  started_at: timestampSchema.nullable(),
  ended_at: timestampSchema.nullable(),
  duration_ms: safeIntegerSchema.nullable(),
  evidence: jsonObjectSchema,
  failure: failureSchema.nullable(),
});

function withSemantics<T extends z.ZodType>(
  schema: T,
  check: (value: z.infer<T>, ctx: z.RefinementCtx) => void,
): T {
  return schema.superRefine(check);
}
const requiresFailure = (status: string) =>
  ["fail", "error", "cancelled", "interrupted", "skipped"].includes(status);
const evalRunSchema = withSemantics(evalRunBase, (value, ctx) => {
  if (requiresFailure(value.status) !== (value.failure !== null))
    ctx.addIssue({
      code: "custom",
      path: ["failure"],
      message: "Failure must match terminal status",
      params: { kensaCode: "contradictory_fields" },
    });
});
const invocationSchema = withSemantics(invocationBase, (value, ctx) => {
  if (requiresFailure(value.status) !== (value.failure !== null))
    ctx.addIssue({
      code: "custom",
      path: ["failure"],
      message: "Failure must match terminal status",
      params: { kensaCode: "contradictory_fields" },
    });
  if (!value.output_recorded && value.output !== null)
    ctx.addIssue({
      code: "custom",
      path: ["output"],
      message: "Unrecorded output must be null",
      params: { kensaCode: "contradictory_fields" },
    });
  if (
    value.evidence_completeness.status === "complete"
      ? value.evidence_completeness.reason !== null
      : value.evidence_completeness.reason === null
  )
    ctx.addIssue({
      code: "custom",
      path: ["evidence_completeness", "reason"],
      message: "Evidence reason contradicts status",
      params: { kensaCode: "contradictory_fields" },
    });
});
const spanSchema = withSemantics(spanBase, (value, ctx) => {
  if ((value.status === "error") !== (value.status_message !== null))
    ctx.addIssue({
      code: "custom",
      path: ["status_message"],
      message: "Error spans require a message",
      params: { kensaCode: "contradictory_fields" },
    });
  if (!value.input_recorded && value.input !== null)
    ctx.addIssue({
      code: "custom",
      path: ["input"],
      message: "Unrecorded input must be null",
      params: { kensaCode: "contradictory_fields" },
    });
  if (!value.output_recorded && value.output !== null)
    ctx.addIssue({
      code: "custom",
      path: ["output"],
      message: "Unrecorded output must be null",
      params: { kensaCode: "contradictory_fields" },
    });
});
const checkResultSchema = withSemantics(checkResultBase, (value, ctx) => {
  if ((value.status === "pass") !== (value.failure === null))
    ctx.addIssue({
      code: "custom",
      path: ["failure"],
      message: "Failure must match check status",
      params: { kensaCode: "contradictory_fields" },
    });
});

export {
  caseSnapshotSchema,
  completenessSchema,
  failureSchema,
  provenanceSchema,
  evalRunSchema,
  invocationSchema,
  spanSchema,
  checkResultSchema,
};
export const protocolDocumentSchema = z.discriminatedUnion("document_kind", [
  evalRunSchema,
  invocationSchema,
  spanSchema,
  checkResultSchema,
]);
type DeepReadonly<T> = T extends readonly (infer Item)[]
  ? readonly DeepReadonly<Item>[]
  : T extends object
    ? { readonly [Key in keyof T]: DeepReadonly<T[Key]> }
    : T;
export type EvalRun = DeepReadonly<z.infer<typeof evalRunSchema>>;
export type Invocation = DeepReadonly<z.infer<typeof invocationSchema>>;
export type Span = DeepReadonly<z.infer<typeof spanSchema>>;
export type CheckResult = DeepReadonly<z.infer<typeof checkResultSchema>>;
export type ProtocolDocument = DeepReadonly<
  z.infer<typeof protocolDocumentSchema>
>;
export type CaseSnapshot = DeepReadonly<z.infer<typeof caseSnapshotSchema>>;
export type Failure = DeepReadonly<z.infer<typeof failureSchema>>;
export type ExecutionProvenance = DeepReadonly<
  z.infer<typeof provenanceSchema>
>;
export type EvidenceCompleteness = DeepReadonly<
  z.infer<typeof completenessSchema>
>;
export type FailureCategory = z.infer<typeof failureCategorySchema>;
export type EffectPolicy = z.infer<typeof effectPolicySchema>;
export type EvidenceStatus = z.infer<typeof evidenceStatusSchema>;
export type EvalRunStatus = EvalRun["status"];
export type InvocationStatus = Invocation["status"];
export type SpanStatus = Span["status"];
export type CheckStatus = CheckResult["status"];

function errorCode(issue: z.core.$ZodIssue): ProtocolErrorCode {
  const params: unknown =
    "params" in issue
      ? (issue as { readonly params?: unknown }).params
      : undefined;
  const custom =
    typeof params === "object" && params !== null && "kensaCode" in params
      ? params.kensaCode
      : undefined;
  if (custom === "contradictory_fields" || custom === "invalid_json_value")
    return custom;
  const path = issue.path.map(String);
  if (path.some((part) => part.endsWith("_at") || part === "created_at"))
    return "invalid_timestamp";
  if (path.some((part) => part === "id" || part.endsWith("_id")))
    return "invalid_identifier";
  if (issue.code === "unrecognized_keys") return "unknown_field";
  if (
    issue.code === "invalid_value" ||
    path.at(-1) === "schema_version" ||
    path.at(-1) === "document_kind"
  )
    return "invalid_literal";
  if (
    path.at(-1) === "duration_ms" ||
    path.at(-1) === "attempt" ||
    issue.code === "too_big" ||
    issue.code === "too_small" ||
    issue.code === "not_multiple_of"
  )
    return "unsafe_integer";
  return "invalid_type";
}
function parse<T>(schema: z.ZodType<T>, value: unknown): T {
  const invalidPath = invalidJsonPath(value);
  if (invalidPath !== null) {
    const topLevelField = invalidPath.length === 1 ? invalidPath[0] : undefined;
    throw new ProtocolError(
      "runtime",
      topLevelField === "duration_ms" || topLevelField === "attempt"
        ? "unsafe_integer"
        : "invalid_json_value",
      invalidPath,
      "Value is not JSON-compatible",
    );
  }
  const result = schema.safeParse(value);
  if (result.success) return result.data;
  const issue = result.error.issues.reduce((first) => first);
  const issuePath =
    issue.code === "unrecognized_keys" &&
    "keys" in issue &&
    Array.isArray(issue.keys) &&
    issue.keys.length > 0
      ? [...issue.path, issue.keys[0]]
      : issue.path;
  throw new ProtocolError(
    "runtime",
    errorCode(issue),
    issuePath.map(String),
    issue.message,
  );
}
export const parseEvalRun = (value: unknown): EvalRun =>
  parse(evalRunSchema, value);
export const parseInvocation = (value: unknown): Invocation =>
  parse(invocationSchema, value);
export const parseSpan = (value: unknown): Span => parse(spanSchema, value);
export const parseCheckResult = (value: unknown): CheckResult =>
  parse(checkResultSchema, value);
export const parseProtocolDocument = (value: unknown): ProtocolDocument =>
  parse(protocolDocumentSchema, value);
export function parseProtocolJson(
  input: string | Uint8Array,
): ProtocolDocument {
  try {
    const text =
      typeof input === "string"
        ? input
        : new TextDecoder("utf-8", { fatal: true }).decode(input);
    return parseProtocolDocument(JSON.parse(text) as unknown);
  } catch (error) {
    if (error instanceof ProtocolError) throw error;
    throw new ProtocolError("syntax", "invalid_json", [], "Invalid JSON");
  }
}
