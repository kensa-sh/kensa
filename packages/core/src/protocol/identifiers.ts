import { z } from "zod";

const uuidV7 =
  "[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}";
const branded = (pattern: RegExp, name: string) =>
  z.string().regex(pattern).brand(name);
export const evalRunIdSchema = branded(
  new RegExp(`^run_${uuidV7}$`),
  "EvalRunId",
);
export const invocationIdSchema = branded(
  new RegExp(`^inv_${uuidV7}$`),
  "InvocationId",
);
export const checkResultIdSchema = branded(
  new RegExp(`^chk_${uuidV7}$`),
  "CheckResultId",
);
export const caseIdSchema = z.string().regex(/\S/).brand("CaseId");
export const traceIdSchema = branded(/^(?!0{32}$)[0-9a-f]{32}$/, "TraceId");
export const spanIdSchema = branded(/^(?!0{16}$)[0-9a-f]{16}$/, "SpanId");
export const nonBlankStringSchema = z.string().regex(/\S/);
export const safeIntegerSchema = z
  .number()
  .int()
  .min(0)
  .max(Number.MAX_SAFE_INTEGER)
  .refine(Number.isSafeInteger);
export const u32Schema = z.number().int().min(0).max(4_294_967_295);

export type EvalRunId = z.infer<typeof evalRunIdSchema>;
export type InvocationId = z.infer<typeof invocationIdSchema>;
export type CheckResultId = z.infer<typeof checkResultIdSchema>;
export type CaseId = z.infer<typeof caseIdSchema>;
export type TraceId = z.infer<typeof traceIdSchema>;
export type SpanId = z.infer<typeof spanIdSchema>;
export type SafeInteger = z.infer<typeof safeIntegerSchema>;
export type U32 = z.infer<typeof u32Schema>;
