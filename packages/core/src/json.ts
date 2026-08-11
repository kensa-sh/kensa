import { z } from "zod";

export type JsonPrimitive = boolean | number | string | null;
export type JsonValue =
  JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

export const jsonValueSchema: z.ZodType<JsonValue> = z.lazy(() =>
  z.union([
    z.null(),
    z.boolean(),
    z
      .number()
      .finite()
      .gte(Number.MIN_SAFE_INTEGER)
      .lte(Number.MAX_SAFE_INTEGER),
    z.string(),
    z.array(jsonValueSchema),
    z
      .custom<Record<string, unknown>>(isPlainObject)
      .pipe(z.record(z.string(), jsonValueSchema)),
  ]),
);

export function canonicalJson(value: unknown): string {
  return canonicalJsonValue(jsonValueSchema.parse(value));
}

function canonicalJsonValue(value: JsonValue): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJsonValue).join(",")}]`;
  }
  const entries = Object.entries(value)
    .sort(([left], [right]) => (left < right ? -1 : 1))
    .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJsonValue(item)}`);
  return `{${entries.join(",")}}`;
}

export async function digestJson(value: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalJson(value));
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const prototype: unknown = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}
