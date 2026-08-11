import { z } from "zod";

import { parseInput } from "./errors.js";

export type JsonPrimitive = boolean | number | string | null;
export type JsonValue =
  JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

export const jsonValueSchema: z.ZodType<JsonValue> = z.custom<JsonValue>(
  (value) => isJsonValue(value, new Set()),
  { message: "value is not interoperable JSON" },
);

export function canonicalJson(value: unknown): string {
  return canonicalJsonValue(parseJsonValue(value));
}

export function parseJsonValue(value: unknown): JsonValue {
  return parseInput(
    jsonValueSchema,
    value,
    "value violates the interoperable JSON contract",
  );
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
  const platform = globalThis as unknown as PlatformGlobals;
  const bytes = new platform.TextEncoder().encode(canonicalJson(value));
  const digest = await platform.crypto.subtle.digest("SHA-256", bytes);
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

function isPreciselyRepresentableNumber(value: number): boolean {
  return !Number.isInteger(value) || Number.isSafeInteger(value);
}

function isJsonValue(value: unknown, ancestors: Set<object>): boolean {
  if (
    value === null ||
    typeof value === "boolean" ||
    typeof value === "string"
  ) {
    return true;
  }
  if (typeof value === "number") {
    return Number.isFinite(value) && isPreciselyRepresentableNumber(value);
  }
  if (!isPlainObject(value) && !Array.isArray(value)) {
    return false;
  }
  if (ancestors.has(value)) {
    return false;
  }
  ancestors.add(value);
  const children = Array.isArray(value) ? value : Object.values(value);
  const valid = children.every((child) => isJsonValue(child, ancestors));
  ancestors.delete(value);
  return valid;
}

interface PlatformGlobals {
  crypto: {
    subtle: {
      digest(algorithm: "SHA-256", data: Uint8Array): Promise<ArrayBuffer>;
    };
  };
  TextEncoder: new () => { encode(input?: string): Uint8Array };
}
