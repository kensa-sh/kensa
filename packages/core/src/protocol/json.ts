export type JsonPrimitive = null | boolean | number | string;
export type JsonValue =
  JsonPrimitive | JsonValue[] | { readonly [key: string]: JsonValue };
export type JsonObject = { readonly [key: string]: JsonValue };
export type JsonPath = readonly (string | number)[];

export function invalidJsonPath(
  value: unknown,
  path: JsonPath = [],
  seen = new Set<object>(),
): JsonPath | null {
  if (value === null || typeof value === "string" || typeof value === "boolean")
    return null;
  if (typeof value === "number")
    return Number.isFinite(value) &&
      (!Number.isInteger(value) || Number.isSafeInteger(value))
      ? null
      : path;
  if (typeof value !== "object") return path;
  if (seen.has(value)) return path;
  seen.add(value);
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index += 1) {
      const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
      if (
        descriptor === undefined ||
        !("value" in descriptor) ||
        !descriptor.enumerable
      )
        return [...path, index];
      const invalid = invalidJsonPath(descriptor.value, [...path, index], seen);
      if (invalid !== null) return invalid;
    }
    for (const key of Reflect.ownKeys(value)) {
      if (key === "length") continue;
      if (
        typeof key !== "string" ||
        !/^(0|[1-9]\d*)$/.test(key) ||
        Number(key) >= value.length
      )
        return [...path, typeof key === "string" ? key : "<symbol>"];
    }
    seen.delete(value);
    return null;
  }
  const prototype = Reflect.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) return path;
  for (const key of Reflect.ownKeys(value)) {
    if (typeof key !== "string") return [...path, "<symbol>"];
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (
      descriptor === undefined ||
      !("value" in descriptor) ||
      !descriptor.enumerable
    )
      return [...path, key];
    const invalid = invalidJsonPath(descriptor.value, [...path, key], seen);
    if (invalid !== null) return invalid;
  }
  seen.delete(value);
  return null;
}

export function isJsonValue(value: unknown): value is JsonValue {
  return invalidJsonPath(value) === null;
}

export function isJsonObject(value: unknown): value is JsonObject {
  return (
    isJsonValue(value) &&
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value)
  );
}
