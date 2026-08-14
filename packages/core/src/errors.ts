import type { ZodError, ZodType } from "zod";

export type CoreErrorCode =
  "invalid_input" | "invalid_transition" | "unsupported_platform";

export type CoreIssueCode =
  | "constraint"
  | "element"
  | "format"
  | "key"
  | "maximum"
  | "minimum"
  | "multiple"
  | "type"
  | "union"
  | "unknown_field"
  | "value";

export type CorePathSegment = string | number;

export interface CoreIssue {
  code: CoreIssueCode;
  message: string;
  path: readonly CorePathSegment[];
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
        code: coreIssueCode(issue.code),
        message: issue.message,
        path: issue.path.map((segment) =>
          typeof segment === "number" ? segment : String(segment),
        ),
      })),
    );
    this.name = "CoreValidationError";
  }
}

function coreIssueCode(
  code: ZodError["issues"][number]["code"],
): CoreIssueCode {
  switch (code) {
    case "custom":
      return "constraint";
    case "invalid_element":
      return "element";
    case "invalid_format":
      return "format";
    case "invalid_key":
      return "key";
    case "too_big":
      return "maximum";
    case "too_small":
      return "minimum";
    case "not_multiple_of":
      return "multiple";
    case "invalid_type":
      return "type";
    case "invalid_union":
      return "union";
    case "unrecognized_keys":
      return "unknown_field";
    case "invalid_value":
      return "value";
    default:
      throw new KensaCoreError(
        "invalid_input",
        "validation produced an unsupported issue code",
      );
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
