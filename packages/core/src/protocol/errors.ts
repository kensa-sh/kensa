export type ProtocolBoundary = "syntax" | "runtime";
export type ProtocolErrorCode =
  | "invalid_json"
  | "invalid_type"
  | "unknown_field"
  | "invalid_literal"
  | "invalid_identifier"
  | "invalid_timestamp"
  | "unsafe_integer"
  | "invalid_json_value"
  | "contradictory_fields";

export class ProtocolError extends Error {
  override readonly name = "ProtocolError";
  constructor(
    readonly boundary: ProtocolBoundary,
    readonly code: ProtocolErrorCode,
    readonly path: readonly (string | number)[],
    message: string,
  ) {
    super(message);
  }
}
