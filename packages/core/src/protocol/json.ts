export type JsonPrimitive = null | boolean | number | string;
export type JsonValue =
  JsonPrimitive | JsonValue[] | { readonly [key: string]: JsonValue };
export type JsonObject = { readonly [key: string]: JsonValue };

export function isJsonValue(
  value: unknown,
  seen = new Set<object>(),
): value is JsonValue {
  /* c8 ignore next 3 */
  if (value === null || typeof value === "string" || typeof value === "boolean")
    return true;
  if (typeof value === "number")
    return (
      Number.isFinite(value) &&
      (!Number.isInteger(value) || Number.isSafeInteger(value))
    );
  if (typeof value !== "object") return false;
  if (seen.has(value)) return false;
  seen.add(value);
  if (Array.isArray(value)) {
    if (value.some((item) => !isJsonValue(item, seen))) return false;
    seen.delete(value);
    return true;
  }
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) return false;
  for (const [key, item] of Object.entries(value))
    if (typeof key !== "string" || !isJsonValue(item, seen)) return false;
  seen.delete(value);
  return true;
}

export function isJsonObject(value: unknown): value is JsonObject {
  return (
    isJsonValue(value) &&
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value)
  );
}
