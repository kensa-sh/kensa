import type { ZodError, ZodType } from "zod";

export type CoreErrorCode = "invalid_input" | "invalid_transition";

export interface CoreIssue {
  code: string;
  message: string;
  path: string;
}

export class KensaCoreError extends Error {
  readonly code: CoreErrorCode;
  readonly issues: readonly CoreIssue[];

  constructor(
    code: CoreErrorCode,
    message: string,
    issues: readonly CoreIssue[] = [],
  ) {
    super(message);
    this.name = "KensaCoreError";
    this.code = code;
    this.issues = issues;
  }
}

export class CoreValidationError extends KensaCoreError {
  constructor(message: string, error: ZodError) {
    super(
      "invalid_input",
      message,
      error.issues.map((issue) => ({
        code: issue.code,
        message: issue.message,
        path: issue.path.map(String).join("."),
      })),
    );
    this.name = "CoreValidationError";
  }
}

export function parseInput<T>(
  schema: ZodType<T>,
  value: unknown,
  message: string,
): T {
  const result = schema.safeParse(value);
  if (!result.success) {
    throw new CoreValidationError(message, result.error);
  }
  return result.data;
}
